package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

var managedTestSecret = []byte("synthetic-managed-send-secret-0123456789")

func managedFixture(t *testing.T) (*managedSender, *textSender, *fixtureSender, string, string) {
	t.Helper()
	w, c, _, path := sendFixture(t)
	ref, err := w.index.accountRef(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return newManagedSender(managedTestSecret, w), w, c, ref, path
}

func signedAt(secret []byte, method, path string, body []byte, at time.Time) *http.Request {
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	stamp := strconv.FormatInt(at.Unix(), 10)
	req.Header.Set(managedTimeHeader, stamp)
	req.Header.Set(managedSigHeader, hex.EncodeToString(managedSignature(secret, method, path, stamp, body)))
	return req
}

func call(h http.Handler, req *http.Request) (int, map[string]any) {
	rw := httptest.NewRecorder()
	h.ServeHTTP(rw, req)
	var out map[string]any
	_ = json.Unmarshal(rw.Body.Bytes(), &out)
	return rw.Code, out
}

func sendBody(chat, text, request, ref string) []byte {
	body, _ := json.Marshal(managedSendInput{RequestID: request, ChatID: chat, Text: text, AccountRef: ref})
	return body
}

func TestManagedRequestsMustBeAuthenticated(t *testing.T) {
	m, _, c, ref, _ := managedFixture(t)
	h := managedHandler(m)
	body := sendBody("friend@s.whatsapp.net", "synthetic", "qorqut-job-auth", ref)
	now := time.Now()
	good := signedAt(managedTestSecret, "POST", "/managed/send", body, now)
	tampered := signedAt(managedTestSecret, "POST", "/managed/send", body, now)
	tampered.Body = httptest.NewRequest("POST", "/", bytes.NewReader(sendBody("friend@s.whatsapp.net", "tampered", "qorqut-job-auth", ref))).Body
	otherPath := signedAt(managedTestSecret, "POST", "/managed/receipt", body, now)
	otherPath.URL.Path = "/managed/send"
	unsigned := httptest.NewRequest("POST", "/managed/send", bytes.NewReader(body))
	badHex := signedAt(managedTestSecret, "POST", "/managed/send", body, now)
	badHex.Header.Set(managedSigHeader, "zz")
	for name, req := range map[string]*http.Request{
		"unsigned":   unsigned,
		"wrong key":  signedAt([]byte("another-secret-with-more-than-32-bytes!"), "POST", "/managed/send", body, now),
		"tampered":   tampered,
		"other path": otherPath,
		"stale":      signedAt(managedTestSecret, "POST", "/managed/send", body, now.Add(-2*time.Minute)),
		"future":     signedAt(managedTestSecret, "POST", "/managed/send", body, now.Add(2*time.Minute)),
		"bad hex":    badHex,
	} {
		if code, out := call(h, req); code != http.StatusUnauthorized || c.calls.Load() != 0 || out["sent"] != false {
			t.Fatalf("%s request was not refused before sending: %d", name, code)
		}
	}
	if code, out := call(h, good); code != http.StatusOK || c.calls.Load() != 1 || out["found"] != true {
		t.Fatalf("authenticated send failed: %d %v", code, out)
	}
}

func TestManagedSendIsOnceAndReceiptOnlyLookupNeverSends(t *testing.T) {
	m, w, c, ref, path := managedFixture(t)
	h := managedHandler(m)
	post := func(route string, body []byte) (int, map[string]any) {
		return call(h, signedAt(managedTestSecret, "POST", route, body, time.Now()))
	}
	lookup := func(request string) (int, map[string]any) {
		body, _ := json.Marshal(managedReceiptInput{RequestID: request, AccountRef: ref})
		return post("/managed/receipt", body)
	}
	// Receipt-only: an unknown request reserves nothing and sends nothing.
	if code, out := lookup("qorqut-job-once"); code != http.StatusOK || out["found"] != false || c.calls.Load() != 0 {
		t.Fatalf("lookup of an unknown request: %d %v", code, out)
	}
	if _, err := w.index.sendRecord(context.Background(), "qorqut-job-once"); err == nil {
		t.Fatal("lookup reserved a request")
	}
	body := sendBody("friend@s.whatsapp.net", "synthetic outgoing", "qorqut-job-once", ref)
	for range 3 {
		code, out := post("/managed/send", body)
		receipt, _ := out["receipt"].(map[string]any)
		if code != http.StatusOK || receipt["state"] != "accepted" || receipt["account_ref"] != ref ||
			receipt["request_id"] != "qorqut-job-once" || receipt["chat_id"] != "friend@s.whatsapp.net" ||
			receipt["fingerprint"] != (sendInput{ChatID: "friend@s.whatsapp.net", Text: "synthetic outgoing"}).fingerprint() {
			t.Fatalf("managed send receipt: %d %v", code, out)
		}
		if _, leaked := receipt["text"]; leaked || strings.Contains(string(mustJSON(out)), "synthetic outgoing") {
			t.Fatal("receipt carried the message text")
		}
	}
	if c.calls.Load() != 1 {
		t.Fatalf("duplicate network sends: %d", c.calls.Load())
	}
	if code, out := lookup("qorqut-job-once"); code != http.StatusOK || out["found"] != true || c.calls.Load() != 1 {
		t.Fatalf("lookup of a recorded request: %d %v", code, out)
	}
	// A restart keeps the receipt and still never resends.
	restarted := reopenSender(t, w, path)
	h = managedHandler(newManagedSender(managedTestSecret, restarted))
	if code, out := post("/managed/send", body); code != http.StatusOK || out["receipt"].(map[string]any)["state"] != "accepted" || c.calls.Load() != 1 {
		t.Fatalf("restart replayed the send: %d %v", code, out)
	}
	// Changed payloads and another account are refused without a send.
	for name, changed := range map[string][]byte{
		"text":    sendBody("friend@s.whatsapp.net", "different", "qorqut-job-once", ref),
		"chat":    sendBody("other@s.whatsapp.net", "synthetic outgoing", "qorqut-job-once", ref),
		"account": sendBody("friend@s.whatsapp.net", "synthetic outgoing", "qorqut-job-once", strings.Repeat("0", 64)),
	} {
		if code, _ := post("/managed/send", changed); code != http.StatusConflict && code != http.StatusBadRequest || c.calls.Load() != 1 {
			t.Fatalf("%s change was not refused: %d", name, code)
		}
	}
	other, _ := json.Marshal(managedReceiptInput{RequestID: "qorqut-job-once", AccountRef: strings.Repeat("0", 64)})
	if code, _ := post("/managed/receipt", other); code != http.StatusConflict {
		t.Fatalf("a lookup for another account was served: %d", code)
	}
}

func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }

func TestManagedUnknownOutcomeStaysUnknownAndIsNotResent(t *testing.T) {
	m, _, c, ref, _ := managedFixture(t)
	h := managedHandler(m)
	c.fail = true
	body := sendBody("friend@s.whatsapp.net", "synthetic outgoing", "qorqut-job-unknown", ref)
	for range 2 {
		code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/send", body, time.Now()))
		receipt, _ := out["receipt"].(map[string]any)
		if code != http.StatusOK || receipt["state"] != "unknown" || c.calls.Load() != 1 {
			t.Fatalf("an uncertain outcome was resent or claimed: %d %v calls=%d", code, out, c.calls.Load())
		}
	}
	look, _ := json.Marshal(managedReceiptInput{RequestID: "qorqut-job-unknown", AccountRef: ref})
	if code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/receipt", look, time.Now())); code != http.StatusOK ||
		out["receipt"].(map[string]any)["state"] != "unknown" || c.calls.Load() != 1 {
		t.Fatalf("lookup changed an unknown outcome: %d %v", code, out)
	}
}

func TestManagedFailureAfterTheSendIsNeverAnExplicitRefusal(t *testing.T) {
	m, w, c, ref, _ := managedFixture(t)
	h := managedHandler(m)
	lost := true
	m.settle = func(ctx context.Context, id string) (sendRecord, bool, error) {
		if lost {
			return sendRecord{}, false, errors.New("cannot inspect send receipt")
		}
		return w.lookup(ctx, id)
	}
	body := sendBody("friend@s.whatsapp.net", "synthetic outgoing", "qorqut-job-after", ref)
	code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/send", body, time.Now()))
	if code == http.StatusOK || code == http.StatusServiceUnavailable || out["sent"] != nil || out["outcome"] != "unknown" || c.calls.Load() != 1 {
		t.Fatalf("a lookup failure after the send looked like a refusal or success: %d %v calls=%d", code, out, c.calls.Load())
	}
	if rec, err := w.index.sendRecord(context.Background(), "qorqut-job-after"); err != nil || rec.State != "accepted" {
		t.Fatalf("the reservation did not survive: %v %v", rec.State, err)
	}
	lost = false
	code, out = call(h, signedAt(managedTestSecret, "POST", "/managed/send", body, time.Now()))
	if receipt, _ := out["receipt"].(map[string]any); code != http.StatusOK || receipt["state"] != "accepted" || c.calls.Load() != 1 {
		t.Fatalf("the same request must return its receipt without sending again: %d %v calls=%d", code, out, c.calls.Load())
	}
}

func TestManagedDisconnectedIsRetryableAndNothingIsReserved(t *testing.T) {
	m, w, c, ref, _ := managedFixture(t)
	h := managedHandler(m)
	c.disconnected = true
	body := sendBody("friend@s.whatsapp.net", "synthetic outgoing", "qorqut-job-offline", ref)
	if code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/send", body, time.Now())); code != http.StatusServiceUnavailable ||
		c.calls.Load() != 0 || out["sent"] != false {
		t.Fatalf("a disconnected send must be an explicit refusal: %d %v", code, out)
	}
	if _, err := w.index.sendRecord(context.Background(), "qorqut-job-offline"); err == nil {
		t.Fatal("a refused send left a reservation")
	}
	bad := sendBody("friend@s.whatsapp.net", " ", "qorqut-job-bad", ref)
	if code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/send", bad, time.Now())); code != http.StatusBadRequest || out["sent"] != false {
		t.Fatalf("an invalid payload: %d %v", code, out)
	}
}

func TestManagedChatLookupIsReadOnly(t *testing.T) {
	m, w, c, _, _ := managedFixture(t)
	h := managedHandler(m)
	if err := w.index.nameChat(context.Background(), "friend@s.whatsapp.net", "Synthetic Friend"); err != nil {
		t.Fatal(err)
	}
	ask := func(chat string) (int, map[string]any) {
		body, _ := json.Marshal(map[string]string{"chat_id": chat})
		return call(h, signedAt(managedTestSecret, "POST", "/managed/chat", body, time.Now()))
	}
	if code, out := ask("friend@s.whatsapp.net"); code != http.StatusOK || out["known"] != true || out["name"] != "Synthetic Friend" {
		t.Fatalf("known chat: %d %v", code, out)
	}
	if code, out := ask("stranger@s.whatsapp.net"); code != http.StatusOK || out["known"] != false || out["name"] != "" {
		t.Fatalf("unknown chat: %d %v", code, out)
	}
	if code, _ := ask("not a chat"); code != http.StatusBadRequest {
		t.Fatalf("invalid chat id accepted: %d", code)
	}
	if c.calls.Load() != 0 {
		t.Fatal("a chat lookup sent something")
	}
}

func TestManagedAccountEndpointAndRoutes(t *testing.T) {
	m, w, _, ref, _ := managedFixture(t)
	h := managedHandler(m)
	code, out := call(h, signedAt(managedTestSecret, "POST", "/managed/account", []byte("{}"), time.Now()))
	if code != http.StatusOK || out["account_ref"] != ref || out["managed_send"] != true {
		t.Fatalf("account endpoint: %d %v", code, out)
	}
	if strings.Contains(string(mustJSON(out)), "owner") {
		t.Fatal("the account reference exposes the account identity")
	}
	again, _ := w.index.accountRef(context.Background())
	if again != ref || len(ref) != 64 {
		t.Fatal("the account reference is not stable")
	}
	for _, route := range []string{"/send", "/status", "/managed", "/managed/other", "/managed/send?x=1"} {
		path := strings.SplitN(route, "?", 2)[0]
		req := signedAt(managedTestSecret, "POST", path, []byte("{}"), time.Now())
		req.URL.RawQuery = strings.TrimPrefix(strings.TrimPrefix(route, path), "?")
		if code, _ := call(h, req); code != http.StatusNotFound {
			t.Fatalf("unexpected managed route %s: %d", route, code)
		}
	}
	if code, _ := call(h, signedAt(managedTestSecret, "GET", "/managed/account", nil, time.Now())); code != http.StatusMethodNotAllowed {
		t.Fatalf("GET on the managed account route: %d", code)
	}
}

func TestAccountReferenceIsOpaqueAndPerAccount(t *testing.T) {
	a, _, _, refA, _ := managedFixture(t)
	b, _, _, refB, _ := managedFixture(t)
	if refA == refB || a == nil || b == nil {
		t.Fatal("two data directories shared one account reference")
	}
	if strings.Contains(refA, "owner") || strings.Contains(refA, "whatsapp") {
		t.Fatal("the account reference is not opaque")
	}
}

func TestReadSocketNeverGainsManagedSending(t *testing.T) {
	w, c, in, _ := sendFixture(t)
	ref, _ := w.index.accountRef(context.Background())
	for _, receiver := range []*receiver{{index: w.index}, {index: w.index, writer: nil}} {
		h := connectorHandler(receiver)
		for _, route := range []string{"/managed/send", "/managed/receipt", "/managed/account", "/send"} {
			body := sendBody(in.ChatID, in.Text, "qorqut-job-read", ref)
			if code, _ := call(h, signedAt(managedTestSecret, "POST", route, body, time.Now())); code != http.StatusForbidden && code != http.StatusMethodNotAllowed && code != http.StatusNotFound {
				t.Fatalf("read socket served %s: %d", route, code)
			}
		}
	}
	if c.calls.Load() != 0 {
		t.Fatal("the read socket reached the send transport")
	}
}

func TestManagedSecretFile(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, data string, mode os.FileMode) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(data), mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
		return path
	}
	good := write("good", string(managedTestSecret)+"\n", 0600)
	if secret, err := loadManagedSecret(good); err != nil || !bytes.Equal(secret, managedTestSecret) {
		t.Fatalf("valid secret rejected: %v", err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(good, link); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{
		"relative":       "secret",
		"missing":        filepath.Join(dir, "missing"),
		"symlink":        link,
		"group readable": write("group", string(managedTestSecret), 0640),
		"world readable": write("world", string(managedTestSecret), 0644),
		"short":          write("short", "too-short", 0600),
		"empty":          write("empty", "", 0600),
		"too long":       write("long", strings.Repeat("x", managedSecretMax+2), 0600),
		"directory":      dir,
	} {
		if _, err := loadManagedSecret(path); err == nil {
			t.Fatalf("%s secret was accepted", name)
		} else if strings.Contains(err.Error(), string(managedTestSecret)) {
			t.Fatal("secret appeared in an error")
		}
	}
}

func TestManagedModeFlagsAreExclusiveAndFailClosed(t *testing.T) {
	dir := t.TempDir()
	base := []string{"serve", "--data-dir", filepath.Join(dir, "data"), "--socket", filepath.Join(dir, "read.sock")}
	managed := []string{"--managed-send-socket", filepath.Join(dir, "send.sock"), "--managed-send-secret-file", filepath.Join(dir, "secret")}
	for name, args := range map[string][]string{
		"with --enable-send":      append(append(append([]string{}, base...), managed...), "--enable-send"),
		"with --pair":             append(append(append([]string{}, base...), managed...), "--pair"),
		"socket without secret":   append(append([]string{}, base...), managed[:2]...),
		"secret without socket":   append(append([]string{}, base...), managed[2:]...),
		"same socket as the read": append(append([]string{}, base...), "--managed-send-socket", filepath.Join(dir, "read.sock"), "--managed-send-secret-file", filepath.Join(dir, "secret")),
		"relative socket":         append(append([]string{}, base...), "--managed-send-socket", "send.sock", "--managed-send-secret-file", filepath.Join(dir, "secret")),
	} {
		if err := run(context.Background(), args); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
	if err := run(context.Background(), []string{"mcp", "--socket", filepath.Join(dir, "read.sock"), "--managed-send-socket", filepath.Join(dir, "send.sock")}); err == nil {
		t.Fatal("the MCP command accepted managed send flags")
	}
	if _, err := os.Stat(filepath.Join(dir, "send.sock")); err == nil {
		t.Fatal("a refused configuration created a managed socket")
	}
}

func TestFingerprintVectorsMatchTheHost(t *testing.T) {
	for _, v := range fingerprintVectors() {
		if got := (sendInput{ChatID: v.chat, Text: v.text}).fingerprint(); got != v.digest {
			t.Fatalf("fingerprint drifted for %q: %s", v.text, got)
		}
	}
}
