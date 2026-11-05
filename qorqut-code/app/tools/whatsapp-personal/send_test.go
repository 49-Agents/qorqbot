package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type fixtureSender struct {
	calls        atomic.Int64
	ids          atomic.Int64
	disconnected bool
	beforeReply  func()
	fail         bool
}

func (c *fixtureSender) IsConnected() bool { return !c.disconnected }
func (c *fixtureSender) IsLoggedIn() bool  { return !c.disconnected }
func (c *fixtureSender) GenerateMessageID() types.MessageID {
	return fmt.Sprintf("synthetic-send-%d", c.ids.Add(1))
}
func (c *fixtureSender) SendMessage(_ context.Context, to types.JID, m *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	c.calls.Add(1)
	if len(extra) != 1 || extra[0].ID == "" || !extra[0].CheckPermissions || m.GetConversation() == "" {
		panic("invalid send transport invocation")
	}
	if c.beforeReply != nil {
		c.beforeReply()
	}
	if c.fail {
		return whatsmeow.SendResponse{}, context.DeadlineExceeded
	}
	return whatsmeow.SendResponse{ID: extra[0].ID, Timestamp: time.Unix(200, 0), Chat: to, Sender: types.NewJID("owner", types.DefaultUserServer)}, nil
}

func sendFixture(t *testing.T) (*textSender, *fixtureSender, sendInput, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "messages.sqlite3")
	s, err := openIndex(path)
	if err != nil {
		t.Fatal(err)
	}
	w := &textSender{index: s}
	t.Cleanup(func() { w.index.db.Close() })
	if err := s.bindAccount(context.Background(), "owner@s.whatsapp.net"); err != nil {
		t.Fatal(err)
	}
	putTest(t, s, message{ChatID: "friend@s.whatsapp.net", ID: "old", Sender: "friend@s.whatsapp.net", Kind: "text", Text: "existing", State: "active", Source: "history", SentAt: 100, version: 100000})
	c := &fixtureSender{}
	w.client = c
	return w, c, sendInput{ChatID: "friend@s.whatsapp.net", Text: "synthetic outgoing", RequestID: "owner-request-1"}, path
}

func reopenSender(t *testing.T, w *textSender, path string) *textSender {
	t.Helper()
	if err := w.index.db.Close(); err != nil {
		t.Fatal(err)
	}
	s, err := openIndex(path)
	if err != nil {
		t.Fatal(err)
	}
	w.index = s
	return &textSender{index: s, client: w.client}
}

func TestSendOnceAndRestart(t *testing.T) {
	ctx := context.Background()
	w, c, in, path := sendFixture(t)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			res, err := w.send(ctx, in)
			if err != nil || res.State != "accepted" || !res.Indexed {
				t.Errorf("send failed: %+v %v", res, err)
			}
		})
	}
	wg.Wait()
	if c.calls.Load() != 1 {
		t.Fatalf("duplicate network sends: %d", c.calls.Load())
	}
	restarted := reopenSender(t, w, path)
	res, err := restarted.send(ctx, in)
	if err != nil || res.State != "accepted" || !res.Indexed || c.calls.Load() != 1 {
		t.Fatalf("restart replayed send: %+v %v", res, err)
	}
	for _, changed := range []sendInput{
		{ChatID: in.ChatID, Text: "different", RequestID: in.RequestID},
		{ChatID: "other@s.whatsapp.net", Text: in.Text, RequestID: in.RequestID},
	} {
		if _, err := restarted.send(ctx, changed); err == nil {
			t.Fatal("accepted request_id with changed payload")
		}
	}
	page, err := w.index.messages(ctx, in.ChatID, "", "", 10)
	if err != nil || len(page.Messages) != 2 || !page.Messages[0].FromMe || page.Messages[0].Text != in.Text || page.Messages[0].SentAt != 200 {
		t.Fatalf("sent message not indexed: %+v %v", page, err)
	}
	changed := page.Messages[0]
	changed.Text, changed.Edited, changed.version = "later edit", true, 200000
	putTest(t, w.index, changed)
	if _, err = restarted.send(ctx, in); err != nil {
		t.Fatal(err)
	}
	page, _ = w.index.messages(ctx, in.ChatID, "", "", 10)
	if page.Messages[0].Text != "later edit" {
		t.Fatal("receipt retry overwrote an equal-timestamp edit")
	}
	changed.State, changed.Kind, changed.Text = "deleted", "deleted", ""
	putTest(t, w.index, changed)
	if _, err = restarted.send(ctx, in); err != nil {
		t.Fatal(err)
	}
	page, _ = w.index.messages(ctx, in.ChatID, "", "", 10)
	if page.Messages[0].State != "deleted" || page.Messages[0].Text != "" || c.calls.Load() != 1 {
		t.Fatal("receipt retry resurrected or resent deleted text")
	}
}

func TestSendUnknownAndEcho(t *testing.T) {
	ctx := context.Background()
	w, c, in, path := sendFixture(t)
	c.fail = true
	res, err := w.send(ctx, in)
	if err != nil || res.State != "unknown" || res.Indexed || c.calls.Load() != 1 {
		t.Fatalf("timeout treated as definite result: %+v %v", res, err)
	}
	restarted := reopenSender(t, w, path)
	res, err = restarted.send(ctx, in)
	if err != nil || res.State != "unknown" || c.calls.Load() != 1 {
		t.Fatalf("unknown send retried after restart: %+v %v", res, err)
	}
	owner := types.NewJID("owner", types.DefaultUserServer)
	pn, _ := types.ParseJID(in.ChatID)
	lid := types.NewJID("friend-lid", types.HiddenUserServer)
	r := &receiver{index: w.index, client: whatsmeow.NewClient(&store.Device{ID: &owner, LIDs: &fixtureLIDs{pn: pn, lid: lid}}, waLog.Noop)}
	event := &events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: lid, Sender: owner, IsFromMe: true},
		ID: res.MessageID, Timestamp: time.Unix(201, 0)}, Message: &waE2E.Message{Conversation: proto.String(in.Text)}}
	// An inbound collision cannot prove that our message was sent.
	event.Info.IsFromMe = false
	r.record(ctx, event, "live", false)
	rec, _ := w.index.sendRecord(ctx, in.RequestID)
	if rec.State != "unknown" {
		t.Fatal("incoming message confirmed an outgoing send")
	}
	event.Info.IsFromMe = true
	r.record(ctx, event, "live", false)
	r.record(ctx, event, "live", false)
	res, err = restarted.send(ctx, in)
	if err != nil || res.State != "accepted" || !res.Indexed || res.SentAt != 201 || c.calls.Load() != 1 {
		t.Fatalf("own-device echo did not resolve receipt: %+v %v", res, err)
	}
	page, _ := w.index.messages(ctx, in.ChatID, "", "", 10)
	if len(page.Messages) != 2 || page.Messages[0].ID != res.MessageID {
		t.Fatalf("alias echo not indexed in requested chat: %+v", page)
	}
}

func TestSendCancelledCallerAndValidation(t *testing.T) {
	w, c, in, _ := sendFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	c.beforeReply = cancel
	res, err := w.send(ctx, in)
	if err != nil || res.State != "accepted" || !res.Indexed {
		t.Fatalf("caller cancellation lost acknowledged send: %+v %v", res, err)
	}
	for _, bad := range []sendInput{
		{ChatID: "unknown@s.whatsapp.net", Text: "text", RequestID: "new"},
		{ChatID: "status@broadcast", Text: "text", RequestID: "new"},
		{ChatID: "name", Text: "text", RequestID: "new"},
		{ChatID: in.ChatID, Text: " ", RequestID: "new"},
		{ChatID: in.ChatID, Text: strings.Repeat("a", 16385), RequestID: "new"},
		{ChatID: in.ChatID, Text: "text", RequestID: "bad id"},
	} {
		if _, err := w.send(context.Background(), bad); err == nil {
			t.Fatal("accepted invalid or unknown recipient send")
		}
	}
	c.disconnected = true
	in.RequestID = "offline"
	if _, err = w.send(context.Background(), in); err == nil {
		t.Fatal("accepted offline new send")
	}
	if c.calls.Load() != 1 {
		t.Fatal("invalid/offline requests reached transport")
	}
	if err := run(context.Background(), []string{"serve", "--socket", "/unused/read.sock", "--pair", "--pair-only", "--enable-send"}); err == nil {
		t.Fatal("pairing-only mode enabled sends")
	}
}

func TestSendHTTPAndMCP(t *testing.T) {
	w, c, in, _ := sendFixture(t)
	body, _ := json.Marshal(in)
	disabled := connectorHandler(&receiver{index: w.index})
	rw := httptest.NewRecorder()
	disabled.ServeHTTP(rw, httptest.NewRequest("POST", "/send", strings.NewReader(string(body))))
	if rw.Code != http.StatusForbidden || c.calls.Load() != 0 {
		t.Fatal("default reader accepted sending")
	}
	h := connectorHandler(&receiver{index: w.index, writer: w})
	for _, invalid := range []string{`{"chat_id":"friend@s.whatsapp.net","text":"x","request_id":"extra","mark_read":true}`, string(body) + `{}`, strings.Repeat("x", 128<<10+1)} {
		rw := httptest.NewRecorder()
		h.ServeHTTP(rw, httptest.NewRequest("POST", "/send", strings.NewReader(invalid)))
		if rw.Code != http.StatusBadRequest || c.calls.Load() != 0 {
			t.Fatal("invalid send JSON reached transport")
		}
	}
	dir, err := os.MkdirTemp("", "wa-send-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	socket := filepath.Join(dir, "api.sock")
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
	ss, err := newMCPServer(socket, true).Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ss.Close()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	list, err := cs.ListTools(ctx, nil)
	if err != nil || len(list.Tools) != 2 {
		t.Fatalf("missing enabled tool: %+v %v", list, err)
	}
	for _, tool := range list.Tools {
		if tool.Name == "whatsapp_send" && (tool.Annotations.ReadOnlyHint || !tool.Annotations.IdempotentHint) {
			t.Fatal("sending mislabeled read-only or non-idempotent")
		}
	}
	for range 2 {
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "whatsapp_send", Arguments: in})
		if err != nil || res.IsError {
			t.Fatalf("MCP send failed: %+v %v", res, err)
		}
		encoded, _ := json.Marshal(res.StructuredContent)
		var receipt sendResult
		if json.Unmarshal(encoded, &receipt) != nil || receipt.State != "accepted" || !receipt.Indexed {
			t.Fatalf("bad receipt: %+v", receipt)
		}
	}
	if c.calls.Load() != 1 {
		t.Fatal("MCP retry sent twice")
	}
	// A stored acknowledgement can repair an index write failure without a send.
	if _, err = w.index.db.Exec("DELETE FROM messages WHERE chat_id=? AND id=?", in.ChatID, "synthetic-send-1"); err != nil {
		t.Fatal(err)
	}
	rec, err := w.send(ctx, in)
	if err != nil || !rec.Indexed || c.calls.Load() != 1 {
		t.Fatalf("failed to repair indexing: %+v %v", rec, err)
	}
	if _, err = w.index.db.Exec("DROP TABLE send_requests"); err != nil {
		t.Fatal(err)
	}
	in.RequestID = "storage-failure"
	if _, err = w.send(ctx, in); err == nil || errors.Is(err, context.Canceled) || c.calls.Load() != 1 {
		t.Fatal("storage failure reached send transport")
	}
}
