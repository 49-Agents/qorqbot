package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

const (
	chatWithMessage = "15550000001@s.whatsapp.net"
	chatNoMessages  = "1203630000000001@g.us"
	chatAbsent      = "15550000002@s.whatsapp.net"
	testAccount     = "15550000009@s.whatsapp.net"
)

// testIndex opens an index in a temp directory with one chat that has a message, one listed chat without messages
// (as WhatsApp's own conversation list creates it) and the linked account bound.
func testIndex(t *testing.T) *index {
	t.Helper()
	idx, err := openIndex(filepath.Join(t.TempDir(), "messages.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { idx.db.Close() })
	ctx := context.Background()
	m := message{ChatID: chatWithMessage, ID: "M1", Sender: chatWithMessage, Kind: "text", Text: "hello", State: "active", Source: "live", SentAt: 1, version: 1}
	if err = idx.put(ctx, m); err != nil {
		t.Fatal(err)
	}
	if err = idx.nameChat(ctx, chatNoMessages, "Test group"); err != nil {
		t.Fatal(err)
	}
	if err = idx.bindAccount(ctx, testAccount); err != nil {
		t.Fatal(err)
	}
	return idx
}

type fakeTransport struct{ calls int }

func (f *fakeTransport) IsConnected() bool                  { return true }
func (f *fakeTransport) IsLoggedIn() bool                   { return true }
func (f *fakeTransport) GenerateMessageID() types.MessageID { return "FAKEMSG1" }
func (f *fakeTransport) SendMessage(_ context.Context, _ types.JID, _ *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	f.calls++
	return whatsmeow.SendResponse{ID: extra[0].ID, Timestamp: time.Now()}, nil
}

func sendRequestCount(t *testing.T, idx *index) int {
	t.Helper()
	var n int
	if err := idx.db.QueryRow("SELECT COUNT(*) FROM send_requests").Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestChatListed(t *testing.T) {
	idx := testIndex(t)
	for id, want := range map[string]bool{chatWithMessage: true, chatNoMessages: true, chatAbsent: false} {
		got, err := idx.chatListed(context.Background(), id)
		if err != nil || got != want {
			t.Errorf("chatListed(%s) = %v, %v; want %v", id, got, err, want)
		}
	}
}

func TestChatListedIgnoresAddressBookNames(t *testing.T) {
	idx := testIndex(t)
	if err := idx.renameChat(context.Background(), chatAbsent, "Someone"); err != nil {
		t.Fatal(err)
	}
	if got, err := idx.chatListed(context.Background(), chatAbsent); err != nil || got {
		t.Errorf("a contact rename must not list a chat: %v, %v", got, err)
	}
}

func TestManagedChatKnown(t *testing.T) {
	idx := testIndex(t)
	secret := bytes.Repeat([]byte("s"), 32)
	handler := managedHandler(newManagedSender(secret, &textSender{index: idx, client: &fakeTransport{}}))
	ask := func(chatID string) map[string]any {
		body, _ := json.Marshal(map[string]string{"chat_id": chatID})
		stamp := strconv.FormatInt(time.Now().Unix(), 10)
		req := httptest.NewRequest(http.MethodPost, "/managed/chat", bytes.NewReader(body))
		req.Header.Set(managedTimeHeader, stamp)
		req.Header.Set(managedSigHeader, hex.EncodeToString(managedSignature(secret, http.MethodPost, "/managed/chat", stamp, body)))
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		var out map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		return out
	}
	if out := ask(chatNoMessages); out["known"] != true || out["name"] != "Test group" {
		t.Errorf("listed chat without messages: %v", out)
	}
	if out := ask(chatWithMessage); out["known"] != true {
		t.Errorf("chat with a message: %v", out)
	}
	if out := ask(chatAbsent); out["known"] != false {
		t.Errorf("absent chat: %v", out)
	}
}

func TestSendListedChatWithoutMessages(t *testing.T) {
	idx := testIndex(t)
	transport := &fakeTransport{}
	s := &textSender{index: idx, client: transport}
	res, err := s.send(context.Background(), sendInput{ChatID: chatNoMessages, Text: "hi", RequestID: "req-1"})
	if err != nil || res.State != "accepted" {
		t.Fatalf("send = %+v, %v", res, err)
	}
	if transport.calls != 1 || sendRequestCount(t, idx) != 1 {
		t.Errorf("calls=%d requests=%d; want 1 and 1", transport.calls, sendRequestCount(t, idx))
	}
}

func TestSendAbsentChatIsRefusedBeforeReserving(t *testing.T) {
	idx := testIndex(t)
	transport := &fakeTransport{}
	s := &textSender{index: idx, client: transport}
	_, err := s.send(context.Background(), sendInput{ChatID: chatAbsent, Text: "hi", RequestID: "req-2"})
	if err == nil || !strings.Contains(err.Error(), "listed by whatsapp_read") {
		t.Fatalf("err = %v; want the chat-list refusal", err)
	}
	if transport.calls != 0 || sendRequestCount(t, idx) != 0 {
		t.Errorf("calls=%d requests=%d; want 0 and 0", transport.calls, sendRequestCount(t, idx))
	}
}

func TestStatusSendingModes(t *testing.T) {
	cases := []struct {
		name     string
		r        func(*index) *receiver
		mode     string
		enabled  bool
		readOnly bool
	}{
		{"off", func(i *index) *receiver { return &receiver{index: i} }, "off", false, true},
		{"managed", func(i *index) *receiver { return &receiver{index: i, managedSend: true} }, "owner_approval", true, true},
		{"direct", func(i *index) *receiver { return &receiver{index: i, writer: &textSender{index: i}} }, "direct", true, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			readHandler(c.r(testIndex(t))).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/status", nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
			}
			var out map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
				t.Fatal(err)
			}
			if out["sending_mode"] != c.mode || out["sending_enabled"] != c.enabled || out["read_only_tools"] != c.readOnly {
				t.Errorf("got mode=%v enabled=%v read_only=%v; want %s %v %v", out["sending_mode"], out["sending_enabled"], out["read_only_tools"], c.mode, c.enabled, c.readOnly)
			}
			if note, _ := out["sending_note"].(string); note == "" {
				t.Error("sending_note is empty")
			}
		})
	}
}
