package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

// Managed sending is the host-controlled alternative to --enable-send. It never touches the read socket or the MCP
// server: sends arrive on a separate private Unix socket and every request carries an HMAC made with an
// operator-created secret file that only the host service reads. The connector validates the request, the frozen
// account reference and the durable receipt; it does not decide who may send (the host's owner Signoff does).
const (
	managedSecretMin  = 32
	managedSecretMax  = 4096
	managedBodyMax    = 128 << 10
	managedClockSkew  = 60 * time.Second
	managedSignPrefix = "qorqut-whatsapp-managed/v1"
	managedTimeHeader = "X-Qorqut-Timestamp"
	managedSigHeader  = "X-Qorqut-Signature"
)

// loadManagedSecret reads the operator-created secret: an absolute path to a real file owned by this user, mode 0600,
// holding 32 to 4096 bytes after a single trailing newline is removed. The value is never logged.
func loadManagedSecret(path string) ([]byte, error) {
	if !filepath.IsAbs(path) {
		return nil, errors.New("the managed send secret path must be absolute")
	}
	info, err := os.Lstat(path)
	if err != nil {
		return nil, errors.New("the managed send secret file is unavailable")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || info.Mode().Perm()&0177 != 0 || !ok || int(stat.Uid) != os.Getuid() {
		return nil, errors.New("the managed send secret must be a regular file owned by this user with mode 0600")
	}
	if info.Size() < managedSecretMin || info.Size() > managedSecretMax+1 {
		return nil, errors.New("the managed send secret must be 32 to 4096 bytes")
	}
	secret, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("the managed send secret file is unavailable")
	}
	if n := len(secret); n > 0 && secret[n-1] == '\n' {
		secret = secret[:n-1]
	}
	if len(secret) < managedSecretMin || len(secret) > managedSecretMax {
		return nil, errors.New("the managed send secret must be 32 to 4096 bytes")
	}
	return secret, nil
}

func managedSignature(secret []byte, method, path, timestamp string, body []byte) []byte {
	digest := sha256.Sum256(body)
	mac := hmac.New(sha256.New, secret)
	for _, part := range []string{managedSignPrefix, method, path, timestamp, hex.EncodeToString(digest[:])} {
		mac.Write([]byte(part))
		mac.Write([]byte{'\n'})
	}
	return mac.Sum(nil)
}

// authenticated verifies the HMAC over method, path, timestamp and body hash in constant time. A failure reveals nothing
// about which part was wrong, and nothing is decoded before it succeeds.
func authenticated(secret []byte, req *http.Request, body []byte, now time.Time) bool {
	seconds, err := strconv.ParseInt(req.Header.Get(managedTimeHeader), 10, 64)
	if err != nil {
		return false
	}
	delta := now.Sub(time.Unix(seconds, 0))
	if delta > managedClockSkew || delta < -managedClockSkew {
		return false
	}
	supplied, err := hex.DecodeString(req.Header.Get(managedSigHeader))
	if err != nil {
		return false
	}
	expected := managedSignature(secret, req.Method, req.URL.Path, req.Header.Get(managedTimeHeader), body)
	return hmac.Equal(supplied, expected)
}

// accountRef is the stable opaque identity of the linked account for this data directory: a hash of the account JID
// with a random per-directory salt, so the phone number never appears in host configuration, receipts or logs. A new
// data directory (a different or re-paired account) yields a different reference.
func (s *index) accountRef(ctx context.Context) (string, error) {
	var account, salt string
	if err := s.db.QueryRowContext(ctx, "SELECT value FROM metadata WHERE key='account'").Scan(&account); err != nil || account == "" {
		return "", errors.New("index is not bound to a linked account")
	}
	if err := s.db.QueryRowContext(ctx, "SELECT value FROM metadata WHERE key='account_salt'").Scan(&salt); errors.Is(err, sql.ErrNoRows) {
		raw := make([]byte, 32)
		if _, err = rand.Read(raw); err != nil {
			return "", err
		}
		if _, err = s.db.ExecContext(ctx, "INSERT OR IGNORE INTO metadata(key,value) VALUES('account_salt',?)", hex.EncodeToString(raw)); err != nil {
			return "", err
		}
		err = s.db.QueryRowContext(ctx, "SELECT value FROM metadata WHERE key='account_salt'").Scan(&salt)
		if err != nil {
			return "", err
		}
	} else if err != nil {
		return "", err
	}
	digest := sha256.New()
	for _, part := range []string{"qorqut-whatsapp-account/v1", salt, account} {
		digest.Write([]byte(part))
		digest.Write([]byte{0})
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

type managedSendInput struct {
	RequestID  string `json:"request_id"`
	ChatID     string `json:"chat_id"`
	Text       string `json:"text"`
	AccountRef string `json:"account_ref"`
}

type managedReceiptInput struct {
	RequestID  string `json:"request_id"`
	AccountRef string `json:"account_ref"`
}

// managedReceipt is a durable send receipt bound to its request, recipient and payload (via the existing fingerprint)
// and to the account. It names the chat ID the host already holds but never carries the message text.
type managedReceipt struct {
	RequestID   string `json:"request_id"`
	ChatID      string `json:"chat_id"`
	MessageID   string `json:"message_id"`
	State       string `json:"state"`
	SentAt      int64  `json:"sent_at,omitempty"`
	Indexed     bool   `json:"indexed"`
	AccountRef  string `json:"account_ref"`
	Fingerprint string `json:"fingerprint"`
}

// lookup is receipt-only: it never reserves, sends or repairs anything.
func (s *textSender) lookup(ctx context.Context, requestID string) (sendRecord, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, err := s.index.sendRecord(ctx, requestID)
	if errors.Is(err, sql.ErrNoRows) {
		return sendRecord{}, false, nil
	}
	if err != nil {
		return sendRecord{}, false, errors.New("cannot inspect send receipt")
	}
	if rec.State == "accepted" {
		if err = s.index.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=? AND id=?)", rec.ChatID, rec.MessageID).Scan(&rec.Indexed); err != nil {
			rec.Indexed = false
		}
	}
	return rec, true, nil
}

func managedReceiptOf(rec sendRecord, ref string) managedReceipt {
	return managedReceipt{RequestID: rec.RequestID, ChatID: rec.ChatID, MessageID: rec.MessageID, State: rec.State,
		SentAt: rec.SentAt, Indexed: rec.Indexed, AccountRef: ref, Fingerprint: rec.fingerprint}
}

type managedSender struct {
	secret []byte
	writer *textSender
	now    func() time.Time
	// settle reads the receipt of a send that just ran. It is the textSender's own lookup; a test replaces it to prove a
	// failure here, after the message may have gone out, is never reported as a refusal.
	settle func(ctx context.Context, requestID string) (sendRecord, bool, error)
}

func newManagedSender(secret []byte, writer *textSender) *managedSender {
	return &managedSender{secret: secret, writer: writer, now: time.Now, settle: writer.lookup}
}

func managedHandler(m *managedSender) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		routes := map[string]string{"/managed/account": http.MethodPost, "/managed/chat": http.MethodPost, "/managed/send": http.MethodPost, "/managed/receipt": http.MethodPost}
		method, ok := routes[req.URL.Path]
		if !ok || req.URL.RawQuery != "" {
			refuse(w, http.StatusNotFound, "not found")
			return
		}
		if req.Method != method {
			refuse(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, req.Body, managedBodyMax))
		if err != nil {
			refuse(w, http.StatusBadRequest, "invalid request")
			return
		}
		if !authenticated(m.secret, req, body, m.now()) {
			refuse(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		ctx, cancel := context.WithTimeout(req.Context(), 25*time.Second)
		defer cancel()
		ref, err := m.writer.index.accountRef(ctx)
		if err != nil {
			refuse(w, http.StatusServiceUnavailable, "account unavailable")
			return
		}
		switch req.URL.Path {
		case "/managed/account":
			if !decodeStrict(body, &struct{}{}) {
				refuse(w, http.StatusBadRequest, "invalid request")
				return
			}
			connected := m.writer.client != nil && m.writer.client.IsConnected() && m.writer.client.IsLoggedIn()
			json.NewEncoder(w).Encode(map[string]any{"account_ref": ref, "connected": connected, "managed_send": true})
		case "/managed/chat":
			// Read-only: whether the exact chat ID is an existing indexed conversation and its known display name, so the
			// owner's Signoff preview can show both. Nothing is reserved or sent.
			var in struct {
				ChatID string `json:"chat_id"`
			}
			if !decodeStrict(body, &in) || (sendInput{ChatID: in.ChatID, Text: "x", RequestID: "x"}).validate() != nil {
				refuse(w, http.StatusBadRequest, "invalid request")
				return
			}
			var known bool
			var name string
			idx := m.writer.index
			if err := idx.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM messages WHERE chat_id=?)", in.ChatID).Scan(&known); err != nil {
				refuse(w, http.StatusServiceUnavailable, "index query failed")
				return
			}
			if err := idx.db.QueryRowContext(ctx, "SELECT name FROM chats WHERE id=?", in.ChatID).Scan(&name); err != nil && !errors.Is(err, sql.ErrNoRows) {
				refuse(w, http.StatusServiceUnavailable, "index query failed")
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"known": known, "name": name})
		case "/managed/receipt":
			var in managedReceiptInput
			if !decodeStrict(body, &in) || !sendRequestID.MatchString(in.RequestID) {
				refuse(w, http.StatusBadRequest, "invalid request")
				return
			}
			if in.AccountRef != ref {
				refuse(w, http.StatusConflict, "account mismatch")
				return
			}
			rec, found, err := m.writer.lookup(ctx, in.RequestID)
			if err != nil {
				refuse(w, http.StatusServiceUnavailable, err.Error())
				return
			}
			if !found {
				json.NewEncoder(w).Encode(map[string]any{"found": false})
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"found": true, "receipt": managedReceiptOf(rec, ref)})
		case "/managed/send":
			var in managedSendInput
			if !decodeStrict(body, &in) {
				refuse(w, http.StatusBadRequest, "invalid request")
				return
			}
			input := sendInput{ChatID: in.ChatID, Text: in.Text, RequestID: in.RequestID}
			if err := input.validate(); err != nil {
				refuse(w, http.StatusBadRequest, err.Error())
				return
			}
			if in.AccountRef != ref {
				refuse(w, http.StatusConflict, "account mismatch")
				return
			}
			result, err := m.writer.send(ctx, input)
			if err != nil {
				// send returns only fixed errors, never upstream payloads or keys. Every error is returned before a
				// request is reserved, so nothing was sent by this call.
				var unavailable unavailableError
				var conflict conflictError
				switch {
				case errors.As(err, &unavailable):
					refuse(w, http.StatusServiceUnavailable, err.Error())
				case errors.As(err, &conflict):
					refuse(w, http.StatusConflict, err.Error())
				default:
					refuse(w, http.StatusBadRequest, err.Error())
				}
				return
			}
			rec, found, err := m.settle(context.WithoutCancel(ctx), result.RequestID)
			if err != nil || !found {
				// send already ran: the message may have gone out. This is deliberately not a refusal (no "sent":false),
				// so the host settles it by receipt-only lookup instead of treating it as proof that nothing was sent.
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]any{"error": "send receipt unavailable", "outcome": "unknown"})
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"found": true, "receipt": managedReceiptOf(rec, ref)})
		}
	})
}

// refuse answers a request that was rejected before any send could happen: authentication, routing, validation, account
// binding, and every error textSender.send returns (all of them precede the durable reservation). The body says so
// explicitly ("sent": false); the host treats any other non-200 answer to a send as an unknown outcome.
func refuse(w http.ResponseWriter, status int, message string) {
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]any{"error": message, "sent": false})
}

func decodeStrict(body []byte, target any) bool {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if decoder.Decode(target) != nil {
		return false
	}
	return decoder.Decode(new(any)) == io.EOF
}
