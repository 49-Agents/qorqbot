package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

type sendInput struct {
	ChatID    string `json:"chat_id" jsonschema:"Exact existing chat ID from whatsapp_read; never a contact name."`
	Text      string `json:"text" jsonschema:"Plain text to send from the owner's personal account, at most 16384 UTF-8 bytes."`
	RequestID string `json:"request_id" jsonschema:"Unique ID for this intended message, 1-128 ASCII letters, digits, dots, underscores, colons or hyphens. Always reuse it with identical arguments after a timeout or retry; repeating a recorded request never sends again."`
}

var sendRequestID = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

func (in sendInput) validate() error {
	jid, err := types.ParseJID(in.ChatID)
	if err != nil || jid.User == "" || jid.ToNonAD() != jid || jid.String() != in.ChatID ||
		(jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer && jid.Server != types.GroupServer) {
		return errors.New("chat_id must be an exact direct or group chat ID from whatsapp_read")
	}
	if !sendRequestID.MatchString(in.RequestID) || !utf8.ValidString(in.Text) || strings.TrimSpace(in.Text) == "" || len(in.Text) > 16384 {
		return errors.New("invalid request_id or text; text must be nonempty and at most 16384 UTF-8 bytes")
	}
	return nil
}

// Failures that happen strictly before a request is reserved. They carry the same fixed text as before and let the
// managed transport tell a retryable "not sent, try again" from a refused request.
type unavailableError string

func (e unavailableError) Error() string { return string(e) }

type conflictError string

func (e conflictError) Error() string { return string(e) }

func (in sendInput) fingerprint() string {
	body, _ := json.Marshal([]string{in.ChatID, in.Text})
	hash := sha256.Sum256(body)
	return hex.EncodeToString(hash[:])
}

type sendResult struct {
	RequestID string `json:"request_id"`
	ChatID    string `json:"chat_id"`
	MessageID string `json:"message_id"`
	State     string `json:"state"` // accepted or unknown; never a delivery/read claim.
	SentAt    int64  `json:"sent_at,omitempty"`
	Indexed   bool   `json:"indexed"`
	Detail    string `json:"detail"`
}

type sendRecord struct {
	sendResult
	fingerprint string
	sender      string
}

type sendTransport interface {
	IsConnected() bool
	IsLoggedIn() bool
	GenerateMessageID() types.MessageID
	SendMessage(context.Context, types.JID, *waE2E.Message, ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error)
}

type textSender struct {
	mu     sync.Mutex
	index  *index
	client sendTransport
}

func (s *index) sendRecord(ctx context.Context, requestID string) (sendRecord, error) {
	var rec sendRecord
	err := s.db.QueryRowContext(ctx, `SELECT request_id,fingerprint,chat_id,message_id,state,sender,sent_at
 FROM send_requests WHERE request_id=?`, requestID).Scan(&rec.RequestID, &rec.fingerprint, &rec.ChatID, &rec.MessageID, &rec.State, &rec.sender, &rec.SentAt)
	return rec, err
}

// Keep no second plaintext copy in the outbox. The request fingerprint binds the
// exact recipient and text; only the ordinary message index stores the content.
func (s *textSender) result(ctx context.Context, rec sendRecord, text string) sendResult {
	if rec.State == "accepted" {
		err := s.index.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=? AND id=?)", rec.ChatID, rec.MessageID).Scan(&rec.Indexed)
		if err == nil && !rec.Indexed {
			m := message{ChatID: rec.ChatID, ID: rec.MessageID, Sender: rec.sender, FromMe: true,
				SentAt: rec.SentAt, Kind: "text", Text: text, State: "active", Source: "live", version: rec.SentAt * 1000}
			// Repair only a missing index entry; never replace later edits or
			// resurrect text that was deleted/omitted after the original send.
			rec.Indexed = s.index.put(ctx, m) == nil
		}
		rec.Detail = "Accepted by WhatsApp or observed in a synced outgoing message; this does not confirm recipient delivery or reading."
		if !rec.Indexed {
			rec.Detail += " Local indexing failed; retry the identical request to repair it without sending again."
		}
	} else {
		rec.Detail = "Send outcome is unknown. This request will not be sent again automatically. Reuse the same request_id and arguments to check it; do not create a new ID to retry an uncertain send."
	}
	return rec.sendResult
}

func (s *textSender) send(ctx context.Context, in sendInput) (sendResult, error) {
	if err := in.validate(); err != nil {
		return sendResult{}, err
	}
	// Serializes reservation and sending, including calls from separate MCP
	// clients. The process lock already enforces one service per data directory.
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return sendResult{}, unavailableError("send request expired before processing; retry with the same request_id")
	}
	rec, err := s.index.sendRecord(ctx, in.RequestID)
	if err == nil {
		if rec.fingerprint != in.fingerprint() {
			return sendResult{}, conflictError("request_id already belongs to different text or recipient")
		}
		return s.result(ctx, rec, in.Text), nil
	} else if !errors.Is(err, sql.ErrNoRows) {
		return sendResult{}, unavailableError("cannot inspect send receipt; nothing was sent by this call")
	}
	var known bool
	if err = s.index.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=?)", in.ChatID).Scan(&known); err != nil || !known {
		return sendResult{}, errors.New("recipient must be an existing indexed conversation; find it using whatsapp_read")
	}
	if s.client == nil || !s.client.IsConnected() || !s.client.IsLoggedIn() {
		return sendResult{}, unavailableError("WhatsApp is disconnected; nothing was sent by this call")
	}
	var owner string
	if err = s.index.db.QueryRowContext(ctx, "SELECT value FROM metadata WHERE key='account'").Scan(&owner); err != nil || owner == "" {
		return sendResult{}, errors.New("index is not bound to a linked account; nothing was sent")
	}
	rec = sendRecord{sendResult: sendResult{RequestID: in.RequestID, ChatID: in.ChatID,
		MessageID: s.client.GenerateMessageID(), State: "unknown"}, fingerprint: in.fingerprint(), sender: owner}
	// Commit before any send. A crash even immediately after this point is
	// conservatively unknown: the next process never blindly replays it.
	_, err = s.index.db.ExecContext(ctx, `INSERT INTO send_requests
 (request_id,fingerprint,chat_id,message_id,state,sender,created_at) VALUES(?,?,?,?,?,?,?)`,
		rec.RequestID, rec.fingerprint, rec.ChatID, rec.MessageID, rec.State, rec.sender, time.Now().Unix())
	if err != nil {
		return sendResult{}, unavailableError("could not reserve send request; nothing was sent by this call")
	}
	jid, _ := types.ParseJID(in.ChatID)
	sendCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	resp, sendErr := s.client.SendMessage(sendCtx, jid, &waE2E.Message{Conversation: proto.String(in.Text)},
		whatsmeow.SendRequestExtra{ID: rec.MessageID, Timeout: 15 * time.Second, CheckPermissions: true})
	// Persist the result even when the caller disconnected while waiting. This
	// context only writes locally; it never retries the network operation.
	saveCtx, saveCancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer saveCancel()
	if sendErr == nil && resp.ID == rec.MessageID && !resp.Timestamp.IsZero() {
		if !resp.Sender.IsEmpty() {
			rec.sender = resp.Sender.ToNonAD().String()
		}
		_, err = s.index.db.ExecContext(saveCtx, `UPDATE send_requests SET state='accepted',sender=?,sent_at=? WHERE request_id=?`,
			rec.sender, resp.Timestamp.Unix(), rec.RequestID)
		if err == nil {
			rec.State, rec.SentAt = "accepted", resp.Timestamp.Unix()
		}
	}
	// A synced outgoing message may have resolved an uncertain outcome while
	// SendMessage was waiting. Never overwrite that evidence with an error.
	if saved, readErr := s.index.sendRecord(saveCtx, in.RequestID); readErr == nil {
		rec = saved
	}
	return s.result(saveCtx, rec, in.Text), nil
}

// Own-device copies can use a phone ID or LID. Match only this account's sent
// message and a locally known chat alias, then keep the requested chat ID.
func (r *receiver) normalizeSentMessage(ctx context.Context, m *message) {
	if !m.FromMe {
		return
	}
	var chatID string
	err := r.index.db.QueryRowContext(ctx, "SELECT chat_id FROM send_requests WHERE message_id=?", m.ID).Scan(&chatID)
	if err == sql.ErrNoRows {
		return
	} else if err != nil {
		r.failed(messageStore)
		return
	}
	if chatID == m.ChatID {
		return
	}
	jid, err := types.ParseJID(m.ChatID)
	if err == nil {
		for _, alias := range r.contactAliases(ctx, jid) {
			if alias.String() == chatID {
				m.ChatID = chatID
				return
			}
		}
	}
}

func (s *index) confirmSentMessage(ctx context.Context, m message) error {
	if !m.FromMe {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `UPDATE send_requests SET state='accepted',sent_at=
 (SELECT sent_at FROM messages WHERE chat_id=? AND id=?),sender=?
 WHERE message_id=? AND chat_id=? AND state='unknown'`, m.ChatID, m.ID, m.Sender, m.ID, m.ChatID)
	return err
}
