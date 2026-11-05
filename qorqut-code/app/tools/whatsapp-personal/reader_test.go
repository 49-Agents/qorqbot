package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAdv"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func testIndex(t *testing.T) *index {
	t.Helper()
	s, err := openIndex(filepath.Join(t.TempDir(), "messages.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.db.Close() })
	return s
}

func putTest(t *testing.T, s *index, m message) {
	t.Helper()
	if err := s.put(context.Background(), m); err != nil {
		t.Fatal(err)
	}
}

func TestHistoryMetadataAndFailureReporting(t *testing.T) {
	s := testIndex(t)
	owner := types.NewJID("owner", types.DefaultUserServer)
	r := &receiver{
		ctx: context.Background(), index: s, fatal: make(chan error, 1),
		client: whatsmeow.NewClient(&store.Device{ID: &owner}, waLog.Noop),
	}
	valid := &waWeb.WebMessageInfo{
		Key:     &waCommon.MessageKey{ID: proto.String("valid"), Participant: proto.String("sender@s.whatsapp.net")},
		Message: &waE2E.Message{Conversation: proto.String("received history fixture")}, MessageTimestamp: proto.Uint64(100),
	}
	malformed := proto.Clone(valid).(*waWeb.WebMessageInfo)
	malformed.Key.ID = proto.String("missing-sender")
	malformed.Key.Participant = nil
	r.event(&events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{
		ID: proto.String("123@g.us"), Messages: []*waHistorySync.HistorySyncMsg{
			nil,
			{Message: &waWeb.WebMessageInfo{Key: &waCommon.MessageKey{ID: proto.String("metadata-only")}}},
			{Message: valid}, {Message: malformed},
		},
	}}}})
	page, err := s.messages(context.Background(), "123@g.us", "", "", 10)
	if err != nil || len(page.Messages) != 1 || page.Messages[0].ID != "valid" {
		t.Fatalf("history content: %+v %v", page, err)
	}
	w := httptest.NewRecorder()
	readHandler(r).ServeHTTP(w, httptest.NewRequest("GET", "/status", nil))
	var status struct {
		Errors  int64            `json:"indexing_errors_since_start"`
		Stages  map[string]int64 `json:"indexing_errors_by_stage_since_start"`
		Batches int64            `json:"history_batches_since_start"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &status); err != nil || w.Code != http.StatusOK {
		t.Fatalf("status response: %s %v", w.Body.String(), err)
	}
	if status.Errors != 1 || status.Stages["history_message_parse"] != 1 || status.Batches != 1 {
		t.Fatalf("metadata counted as errors or parse error hidden: %+v", status)
	}
	if err := s.db.Close(); err != nil {
		t.Fatal(err)
	}
	r.record(context.Background(), &events.Message{
		Info:    types.MessageInfo{MessageSource: types.MessageSource{Chat: owner}, ID: "storage-failure", Timestamp: time.Unix(100, 0)},
		Message: &waE2E.Message{Conversation: proto.String("storage fixture")},
	}, "live", false)
	counts := r.errorCounts()
	if r.errors.Load() != 2 || counts["message_store"] != 1 || counts["history_message_parse"] != 1 || counts["history_chat_parse"] != 0 || counts["chat_store"] != 0 {
		t.Fatalf("failure stages were conflated: %+v", counts)
	}
}

func TestIndexReplayEditDeleteAndRestart(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "messages.sqlite3")
	s, err := openIndex(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { s.db.Close() }()
	if err = s.bindAccount(ctx, "owner@s.whatsapp.net"); err != nil {
		t.Fatal(err)
	}
	if err = s.bindAccount(ctx, "different@s.whatsapp.net"); err == nil {
		t.Fatal("accepted another account")
	}
	m := message{ChatID: "friend@s.whatsapp.net", ID: "message-1", Sender: "friend@s.whatsapp.net", Text: "original private text", Kind: "text", State: "active", Source: "history", SentAt: 100, version: 100000}
	putTest(t, s, m)
	putTest(t, s, m)
	m.Source = "live"
	putTest(t, s, m)
	page, err := s.changes(ctx, 0, 100)
	if err != nil || len(page.Changes) != 1 {
		t.Fatalf("duplicate sync: %+v %v", page, err)
	}
	old := m
	old.Source = "history"
	old.Text = "stale history"
	putTest(t, s, old)
	m.Text = "edited private text"
	m.Edited = true
	m.version = 200000
	m.SentAt = 200
	putTest(t, s, m)
	putTest(t, s, old)
	msgs, err := s.messages(ctx, m.ChatID, "", "", 100)
	if err != nil || len(msgs.Messages) != 1 || msgs.Messages[0].Text != m.Text || msgs.Messages[0].SentAt != 100 {
		t.Fatalf("edit order: %+v %v", msgs, err)
	}
	deleted := m
	deleted.State = "deleted"
	deleted.Kind = "deleted"
	deleted.Text = ""
	deleted.version = 300000
	putTest(t, s, deleted)
	putTest(t, s, old)
	putTest(t, s, deleted)
	if err = s.db.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = openIndex(path)
	if err != nil {
		t.Fatal(err)
	}
	page, err = s.changes(ctx, 1, 1)
	if err != nil || !page.HasMore || page.Next != 2 || page.Changes[0].Message.State != "deleted" || page.Changes[0].Message.Text != "" {
		t.Fatalf("cursor/current tombstone: %+v %v", page, err)
	}
	last, err := s.changes(ctx, page.Next, 100)
	if err != nil || len(last.Changes) != 1 || last.Next != 3 || last.HasMore {
		t.Fatalf("restart cursor: %+v %v", last, err)
	}
	msgs, err = s.messages(ctx, "", "private", "", 100)
	if err != nil || len(msgs.Messages) != 0 {
		t.Fatalf("deleted content searchable: %+v %v", msgs, err)
	}
}

func TestOmittedContentCannotBeRestoredBySync(t *testing.T) {
	s := testIndex(t)
	m := message{ChatID: "chat", ID: "id", Text: "secret", State: "active", Kind: "text", Source: "live", SentAt: 100, version: 100000}
	putTest(t, s, m)
	omitted := m
	omitted.State = "omitted"
	omitted.Kind = "ephemeral_or_view_once"
	omitted.Text = ""
	omitted.Source = "history"
	putTest(t, s, omitted)
	putTest(t, s, omitted)
	m.version = 200000
	putTest(t, s, m)
	page, err := s.messages(context.Background(), "chat", "", "", 10)
	if err != nil || len(page.Messages) != 1 || page.Messages[0].State != "omitted" || page.Messages[0].Text != "" {
		t.Fatalf("restored omitted content: %+v %v", page, err)
	}
	changes, err := s.changes(context.Background(), 0, 100)
	if err != nil || len(changes.Changes) != 2 {
		t.Fatalf("duplicate omission: %+v %v", changes, err)
	}
}

func TestStablePaginationAndLiteralSearch(t *testing.T) {
	s := testIndex(t)
	for _, id := range []string{"a", "b", "c"} {
		putTest(t, s, message{ChatID: "chat", ID: id, Text: "100% ready", State: "active", Kind: "text", Source: "history", SentAt: 100, version: 100000})
	}
	page, err := s.messages(context.Background(), "chat", "%", "", 2)
	if err != nil || len(page.Messages) != 2 || page.Next == "" {
		t.Fatalf("first page: %+v %v", page, err)
	}
	next, err := s.messages(context.Background(), "chat", "%", page.Next, 2)
	if err != nil || len(next.Messages) != 1 || next.Messages[0].ID != "a" || next.Next != "" {
		t.Fatalf("second page: %+v %v", next, err)
	}
	if _, err = s.messages(context.Background(), "chat", "", "not-a-cursor", 2); err == nil {
		t.Fatal("accepted malformed cursor")
	}
	if err = s.nameChat(context.Background(), "chat", "A Friend"); err != nil {
		t.Fatal(err)
	}
	chats, err := s.chats(context.Background(), "friend", "", 10)
	if err != nil || len(chats.Chats) != 1 || chats.Chats[0].LastAt != 100 {
		t.Fatalf("chat lookup: %+v %v", chats, err)
	}
}

func TestExtractEditsAndPrivateContent(t *testing.T) {
	base := events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: types.NewJID("friend", types.DefaultUserServer), Sender: types.NewJID("friend", types.DefaultUserServer)}, ID: "envelope", Timestamp: time.Unix(100, 0)}}
	for _, tc := range []struct {
		name                  string
		raw                   *waE2E.Message
		state, kind, id, text string
	}{
		{"text", &waE2E.Message{Conversation: proto.String("hello")}, "active", "text", "envelope", "hello"},
		{"caption", &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("a caption")}}, "active", "image", "envelope", "a caption"},
		{"ephemeral", &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: &waE2E.Message{Conversation: proto.String("secret")}}}, "omitted", "ephemeral_or_view_once", "envelope", ""},
		{"expiration", &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("secret"), ContextInfo: &waE2E.ContextInfo{Expiration: proto.Uint32(60)}}}, "omitted", "ephemeral_or_view_once", "envelope", ""},
		{"view once flag", &waE2E.Message{ImageMessage: &waE2E.ImageMessage{Caption: proto.String("secret"), ViewOnce: proto.Bool(true)}}, "omitted", "ephemeral_or_view_once", "envelope", ""},
		{"edit", &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("original")}, TimestampMS: proto.Int64(200000), EditedMessage: &waE2E.Message{Conversation: proto.String("changed")}}}, "active", "text", "original", "changed"},
		{"delete", &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: &waCommon.MessageKey{ID: proto.String("original")}}}, "deleted", "deleted", "original", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := base
			e.RawMessage = tc.raw
			m, ok := extract(&e, "history", false)
			if !ok || m.State != tc.state || m.Kind != tc.kind || m.ID != tc.id || m.Text != tc.text {
				t.Fatalf("bad extraction: %+v %v", m, ok)
			}
			if tc.name == "edit" && (!m.Edited || m.version != 200000) {
				t.Fatalf("lost edit version: %+v", m)
			}
		})
	}
}

func TestExtractIncomingAndOwnDeviceCopies(t *testing.T) {
	for _, fromMe := range []bool{false, true} {
		e := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{
			Chat: types.NewJID("friend", types.DefaultUserServer), Sender: types.NewJID("sender", types.DefaultUserServer),
			IsFromMe: fromMe}, ID: "same-id", Timestamp: time.Unix(100, 0)},
			RawMessage: &waE2E.Message{Conversation: proto.String("synthetic text")}}
		m, ok := extract(e, "live", false)
		if !ok || m.FromMe != fromMe || m.ChatID != "friend@s.whatsapp.net" || m.Text != "synthetic text" {
			t.Fatalf("lost direction or chat: %+v", m)
		}
	}
}

func TestPairOnlyRequiresExplicitFlag(t *testing.T) {
	if err := run(context.Background(), []string{"serve", "--socket", "/unused/read.sock", "--pair-only"}); err == nil {
		t.Fatal("pair-only accepted without the pairing flag")
	}
}

func TestPairOnlyAlreadyLinkedDoesNotConnect(t *testing.T) {
	ctx := context.Background()
	data := t.TempDir()
	if err := os.Chmod(data, 0700); err != nil {
		t.Fatal(err)
	}
	container, err := sqlstore.New(ctx, "sqlite3", sqliteURL(filepath.Join(data, "session.sqlite3")), waLog.Noop)
	if err != nil {
		t.Fatal(err)
	}
	device := container.NewDevice()
	jid := types.NewJID("synthetic", types.DefaultUserServer)
	device.ID = &jid
	device.Account = &waAdv.ADVSignedDeviceIdentity{Details: []byte{}, AccountSignature: make([]byte, 64), AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64)}
	if err = device.Save(ctx); err != nil {
		t.Fatal(err)
	}
	container.Close()
	qr := filepath.Join(data, "pair.png")
	if err = os.WriteFile(qr, []byte("expired"), 0600); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(data, "pair.sock")
	if err = serve(ctx, data, socket, true, true, false, managedSend{}); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{qr, socket} {
		if _, err = os.Lstat(path); !os.IsNotExist(err) {
			t.Fatalf("left pairing artifact %s: %v", path, err)
		}
	}
}

func TestReadOnlyHTTPAndMCP(t *testing.T) {
	s := testIndex(t)
	putTest(t, s, message{ChatID: "chat", ID: "id", Kind: "text", Text: "hello", State: "active", Source: "live", SentAt: 100, version: 100000})
	h := readHandler(&receiver{index: s})
	for _, tc := range []struct {
		method, path string
		status       int
	}{
		{"POST", "/messages", 405}, {"GET", "/send", 404}, {"GET", "/pair", 404},
		{"GET", "/messages", 400}, {"GET", "/messages?chat_id=chat&limit=-1", 400},
		{"GET", "/changes?after=-1", 400}, {"GET", "/changes?after=broken", 400}, {"GET", "/chats?limit=101", 400},
		{"GET", "/messages?chat_id=chat&before=broken", 400},
	} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(tc.method, tc.path, nil))
		if w.Code != tc.status {
			t.Errorf("%s %s: %d", tc.method, tc.path, w.Code)
		}
	}
	dir, err := os.MkdirTemp("", "wa-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	socket := filepath.Join(dir, "read.sock")
	l, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	hs := &http.Server{Handler: h}
	go hs.Serve(l)
	defer hs.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ct, st := mcp.NewInMemoryTransports()
	server := newMCPServer(socket, false)
	ss, err := server.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ss.Close()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	list, err := cs.ListTools(ctx, nil)
	if err != nil || len(list.Tools) != 1 || list.Tools[0].Name != "whatsapp_read" || !list.Tools[0].Annotations.ReadOnlyHint {
		t.Fatalf("unexpected tools: %+v %v", list, err)
	}
	for _, action := range []string{"status", "chats", "messages", "changes"} {
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "whatsapp_read", Arguments: map[string]any{"action": action, "chat_id": "chat"}})
		if err != nil || res.IsError {
			t.Fatalf("%s: %+v %v", action, res, err)
		}
		if action == "messages" {
			body, _ := json.Marshal(res)
			if !strings.Contains(string(body), "hello") {
				t.Fatalf("missing body: %s", body)
			}
		}
	}
	for _, action := range []string{"send", "mark_read", "../pair"} {
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "whatsapp_read", Arguments: map[string]any{"action": action}})
		if err == nil && !res.IsError {
			t.Fatalf("accepted forbidden action %s", action)
		}
	}
	page, err := s.changes(ctx, 0, 100)
	if err != nil || len(page.Changes) != 1 {
		t.Fatalf("reads changed the index: %+v %v", page, err)
	}
}
