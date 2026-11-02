package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/url"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

type message struct {
	ChatID     string `json:"chat_id"`
	ID         string `json:"id"`
	Sender     string `json:"sender"`
	SenderName string `json:"sender_name"`
	FromMe     bool   `json:"from_me"`
	SentAt     int64  `json:"sent_at"` // Unix seconds, as reported by WhatsApp.
	Kind       string `json:"kind"`
	Text       string `json:"text"`
	State      string `json:"state"`
	Edited     bool   `json:"edited"`
	Source     string `json:"source"`
	version    int64
}

type index struct{ db *sql.DB }

func sqliteURL(path string) string {
	u := url.URL{Scheme: "file", Path: path}
	u.RawQuery = "_foreign_keys=on&_busy_timeout=5000&_journal_mode=WAL&_secure_delete=on"
	return u.String()
}

func openIndex(path string) (*index, error) {
	db, err := sql.Open("sqlite3", sqliteURL(path))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS messages (
 chat_id TEXT NOT NULL, id TEXT NOT NULL, sender TEXT NOT NULL, sender_name TEXT NOT NULL,
 from_me INTEGER NOT NULL, sent_at INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
 state TEXT NOT NULL, edited INTEGER NOT NULL, source TEXT NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY(chat_id,id));
CREATE INDEX IF NOT EXISTS messages_time ON messages(sent_at DESC,chat_id DESC,id DESC);
CREATE TABLE IF NOT EXISTS changes (
 cursor INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
 recorded_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS send_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, chat_id TEXT NOT NULL,
 message_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, sender TEXT NOT NULL,
 created_at INTEGER NOT NULL, sent_at INTEGER NOT NULL DEFAULT 0);
`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &index{db}, nil
}

func (s *index) bindAccount(ctx context.Context, id string) error {
	if id == "" {
		return errors.New("account is not linked")
	}
	_, err := s.db.ExecContext(ctx, "INSERT OR IGNORE INTO metadata(key,value) VALUES('account',?)", id)
	if err != nil {
		return err
	}
	var existing string
	if err = s.db.QueryRowContext(ctx, "SELECT value FROM metadata WHERE key='account'").Scan(&existing); err != nil {
		return err
	}
	if existing != id {
		return errors.New("this data directory belongs to another WhatsApp account; use a new private directory")
	}
	return nil
}

func (s *index) nameChat(ctx context.Context, id, name string) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO chats(id,name) VALUES(?,?) ON CONFLICT(id)
 DO UPDATE SET name=excluded.name WHERE excluded.name<>''`, id, name)
	return err
}

// Contact synchronization only renames known chats; it must not turn the
// address book into empty conversations in the message index.
func (s *index) renameChat(ctx context.Context, id, name string) error {
	if name == "" {
		return nil
	}
	_, err := s.db.ExecContext(ctx, "UPDATE chats SET name=? WHERE id=? AND name<>?", name, id, name)
	return err
}

func (s *index) put(ctx context.Context, m message) error {
	if m.ID == "" || m.ChatID == "" {
		return errors.New("message has no identity")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "INSERT OR IGNORE INTO chats(id) VALUES(?)", m.ChatID); err != nil {
		return err
	}
	// Tombstones cannot be resurrected by delayed history. An edit keeps the
	// original message time; its independent version determines update order.
	result, err := tx.ExecContext(ctx, `INSERT INTO messages
 (chat_id,id,sender,sender_name,from_me,sent_at,kind,text,state,edited,source,version)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,id) DO UPDATE SET
 kind=excluded.kind,text=excluded.text,state=excluded.state,edited=excluded.edited,
 source=excluded.source,version=excluded.version
 WHERE messages.state<>'deleted' AND
 ((excluded.state IN ('deleted','omitted') AND messages.state<>excluded.state) OR (messages.state<>'omitted' AND excluded.version>=messages.version AND
 NOT (messages.source='live' AND excluded.source='history' AND excluded.version=messages.version) AND
 (messages.text<>excluded.text OR messages.kind<>excluded.kind OR messages.state<>excluded.state
 OR messages.edited<>excluded.edited)))`,
		m.ChatID, m.ID, m.Sender, m.SenderName, m.FromMe, m.SentAt, m.Kind, m.Text,
		m.State, m.Edited, m.Source, m.version)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n > 0 {
		_, err = tx.ExecContext(ctx, "INSERT INTO changes(chat_id,message_id,recorded_at) VALUES(?,?,?)", m.ChatID, m.ID, time.Now().Unix())
		if err != nil {
			return err
		}
	} else {
		// A live echo of history promotes the source without producing a second
		// message change. Equal-content newer edits still advance the version.
		_, err = tx.ExecContext(ctx, `UPDATE messages SET source=CASE WHEN ?='live' THEN 'live' ELSE source END,
 version=MAX(version,?) WHERE chat_id=? AND id=? AND state=? AND state='active' AND kind=? AND text=? AND edited=?`,
			m.Source, m.version, m.ChatID, m.ID, m.State, m.Kind, m.Text, m.Edited)
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

const messageColumns = "m.chat_id,m.id,m.sender,m.sender_name,m.from_me,m.sent_at,m.kind,m.text,m.state,m.edited,m.source"

func scanMessage(rows interface{ Scan(...any) error }, m *message, prefix ...any) error {
	return rows.Scan(append(prefix, &m.ChatID, &m.ID, &m.Sender, &m.SenderName, &m.FromMe,
		&m.SentAt, &m.Kind, &m.Text, &m.State, &m.Edited, &m.Source)...)
}

type messagePage struct {
	Messages []message `json:"messages"`
	Next     string    `json:"next_before,omitempty"`
}

type pageKey struct {
	Time int64
	Chat string
	ID   string
}

func (s *index) messages(ctx context.Context, chat, query, before string, limit int) (messagePage, error) {
	page := messagePage{Messages: []message{}}
	where, args := []string{"1=1"}, []any{}
	if chat != "" {
		where, args = append(where, "m.chat_id=?"), append(args, chat)
	}
	if query != "" {
		where, args = append(where, "instr(lower(m.text),lower(?))>0"), append(args, query)
	}
	if before != "" {
		var key pageKey
		body, err := base64.RawURLEncoding.DecodeString(before)
		if err != nil || json.Unmarshal(body, &key) != nil || key.ID == "" || key.Chat == "" {
			return page, errors.New("invalid before cursor")
		}
		where, args = append(where, "(m.sent_at,m.chat_id,m.id)<(?,?,?)"), append(args, key.Time, key.Chat, key.ID)
	}
	args = append(args, limit+1)
	rows, err := s.db.QueryContext(ctx, "SELECT "+messageColumns+" FROM messages m WHERE "+strings.Join(where, " AND ")+" ORDER BY m.sent_at DESC,m.chat_id DESC,m.id DESC LIMIT ?", args...)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var m message
		if err = scanMessage(rows, &m); err != nil {
			return page, err
		}
		page.Messages = append(page.Messages, m)
	}
	if len(page.Messages) > limit {
		page.Messages = page.Messages[:limit]
		last := page.Messages[limit-1]
		body, _ := json.Marshal(pageKey{last.SentAt, last.ChatID, last.ID})
		page.Next = base64.RawURLEncoding.EncodeToString(body)
	}
	return page, rows.Err()
}

type chat struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	LastAt int64  `json:"last_message_at"`
}

type chatPage struct {
	Chats []chat `json:"chats"`
	Next  string `json:"next_after,omitempty"`
}

func (s *index) chats(ctx context.Context, query, after string, limit int) (chatPage, error) {
	page := chatPage{Chats: []chat{}}
	rows, err := s.db.QueryContext(ctx, `SELECT c.id,c.name,COALESCE(MAX(m.sent_at),0) FROM chats c
 LEFT JOIN messages m ON m.chat_id=c.id WHERE c.id>? AND (instr(lower(c.name),lower(?))>0 OR instr(c.id,?)>0)
 GROUP BY c.id ORDER BY c.id LIMIT ?`, after, query, query, limit+1)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var c chat
		if err = rows.Scan(&c.ID, &c.Name, &c.LastAt); err != nil {
			return page, err
		}
		page.Chats = append(page.Chats, c)
	}
	if len(page.Chats) > limit {
		page.Chats = page.Chats[:limit]
		page.Next = page.Chats[limit-1].ID
	}
	return page, rows.Err()
}

type change struct {
	Cursor     int64   `json:"cursor"`
	RecordedAt int64   `json:"recorded_at"`
	Message    message `json:"message"` // Current state, never a retained copy of deleted text.
}

type changePage struct {
	Changes []change `json:"changes"`
	Next    int64    `json:"next_after"`
	HasMore bool     `json:"has_more"`
}

func (s *index) changes(ctx context.Context, after int64, limit int) (changePage, error) {
	page := changePage{Changes: []change{}, Next: after}
	rows, err := s.db.QueryContext(ctx, "SELECT c.cursor,c.recorded_at,"+messageColumns+` FROM changes c
 JOIN messages m ON m.chat_id=c.chat_id AND m.id=c.message_id WHERE c.cursor>? ORDER BY c.cursor LIMIT ?`, after, limit+1)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var c change
		if err = scanMessage(rows, &c.Message, &c.Cursor, &c.RecordedAt); err != nil {
			return page, err
		}
		page.Changes = append(page.Changes, c)
	}
	if len(page.Changes) > limit {
		page.HasMore = true
		page.Changes = page.Changes[:limit]
	}
	if len(page.Changes) > 0 {
		page.Next = page.Changes[len(page.Changes)-1].Cursor
	}
	return page, rows.Err()
}
