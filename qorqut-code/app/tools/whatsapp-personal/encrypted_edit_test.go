package main

import (
	"context"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type fixtureSecrets struct {
	store.MsgSecretStore
	chat, sender types.JID
	secret       []byte
	reads        int
}

func (s *fixtureSecrets) GetMessageSecret(_ context.Context, chat, sender types.JID, id types.MessageID) ([]byte, types.JID, error) {
	s.reads++
	if chat == s.chat && sender == s.sender && id == "original" {
		return s.secret, s.sender, nil
	}
	return nil, types.EmptyJID, nil
}

func TestEncryptedEdits(t *testing.T) {
	for _, fromMe := range []bool{false, true} {
		name := "incoming"
		if fromMe {
			name = "own-device-copy"
		}
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			s := testIndex(t)
			chat := types.NewJID("123", types.DefaultUserServer)
			sender := types.NewJID("456", types.HiddenUserServer)
			secrets := &fixtureSecrets{chat: chat, sender: sender, secret: []byte("synthetic-message-secret-32-bytes")}
			client := whatsmeow.NewClient(&store.Device{ID: &sender, MsgSecrets: secrets}, waLog.Noop)
			r := &receiver{index: s, client: client}
			putTest(t, s, message{ChatID: chat.String(), ID: "original", Sender: sender.String(), FromMe: fromMe,
				Text: "before", Kind: "text", State: "active", Source: "live", SentAt: 100, version: 100000})
			key := &waCommon.MessageKey{ID: proto.String("original"), FromMe: proto.Bool(true)}
			body, err := proto.Marshal(&waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
				Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: key, TimestampMS: proto.Int64(200000),
				EditedMessage: &waE2E.Message{Conversation: proto.String("after")},
			}})
			if err != nil {
				t.Fatal(err)
			}
			// Generate an authenticated fixture using the pinned library, with no
			// connection, real account or network calls.
			ciphertext, iv, err := client.DangerousInternals().EncryptMsgSecret(ctx, sender, chat, sender, "original", whatsmeow.EncSecretMessageEdit, body)
			if err != nil {
				t.Fatal(err)
			}
			makeEvent := func(payload []byte) *events.Message {
				raw := &waE2E.Message{SecretEncryptedMessage: &waE2E.SecretEncryptedMessage{
					SecretEncType: waE2E.SecretEncryptedMessage_MESSAGE_EDIT.Enum(), TargetMessageKey: key,
					EncPayload: payload, EncIV: iv,
				}}
				if fromMe {
					raw = &waE2E.Message{DeviceSentMessage: &waE2E.DeviceSentMessage{Message: raw}}
				}
				return &events.Message{
					Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsFromMe: fromMe},
						ID: "edit-envelope", Timestamp: time.Unix(200, 0)}, RawMessage: raw,
				}
			}
			evt := makeEvent(ciphertext)
			r.record(ctx, evt, "live", false)
			r.record(ctx, evt, "live", false)
			page, err := s.messages(ctx, chat.String(), "", "", 10)
			if err != nil || len(page.Messages) != 1 || page.Messages[0].ID != "original" || page.Messages[0].Text != "after" ||
				!page.Messages[0].Edited || page.Messages[0].SentAt != 100 || page.Messages[0].FromMe != fromMe {
				t.Fatalf("encrypted edit did not update original: %+v %v", page, err)
			}
			if evt.Info.ID != "edit-envelope" || evt.IsEdit || evt.Message != nil {
				t.Fatal("mutated upstream event")
			}
			changes, err := s.changes(ctx, 0, 10)
			if err != nil || len(changes.Changes) != 2 {
				t.Fatalf("duplicate encrypted delivery created changes: %+v %v", changes, err)
			}
			corrupt := append([]byte(nil), ciphertext...)
			corrupt[len(corrupt)-1] ^= 1
			r.record(ctx, makeEvent(corrupt), "live", false)
			page, err = s.messages(ctx, chat.String(), "", "", 10)
			if err != nil || len(page.Messages) != 1 || page.Messages[0].Text != "after" || r.errors.Load() != 1 || r.errorCounts()["encrypted_edit_decrypt"] != 1 {
				t.Fatalf("failed decryption changed content or was silent: %+v %v", page, err)
			}
			beforeReads := secrets.reads
			r.record(ctx, evt, "history", true)
			page, err = s.messages(ctx, chat.String(), "", "", 10)
			if err != nil || len(page.Messages) != 1 || page.Messages[0].State != "omitted" || page.Messages[0].Text != "" || secrets.reads != beforeReads {
				t.Fatalf("omitted content was decrypted or retained: %+v %v", page, err)
			}
		})
	}
}
