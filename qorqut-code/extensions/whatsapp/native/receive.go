package main

import (
	"context"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type indexErrorStage int

const (
	historyChatParse indexErrorStage = iota
	historyMessageParse
	messageStore
	chatStore
	encryptedEditDecrypt
	contactLookup
	indexErrorStageCount
)

var indexErrorStageNames = [...]string{"history_chat_parse", "history_message_parse", "message_store", "chat_store", "encrypted_edit_decrypt", "contact_lookup"}

type receiver struct {
	ctx            context.Context
	mu             sync.Mutex
	stopped        bool
	index          *index
	client         *whatsmeow.Client
	writer         *textSender
	errors         atomic.Int64
	errorsByStage  [indexErrorStageCount]atomic.Int64
	historyBatches atomic.Int64
	lastReceived   atomic.Int64
	fatal          chan error
}

func (r *receiver) stop() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stopped = true
}

// Omit ephemeral/view-once content, including nested messages and media flags.
// Do not keep the raw protobuf: it contains media keys and possibly quoted text.
func privateContent(m protoreflect.Message, depth int) bool {
	if !m.IsValid() {
		return false
	}
	if depth > 20 {
		return true
	}
	omit := false
	m.Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(f.Name())
		if name == "ephemeralMessage" || name == "viewOnceMessage" || name == "viewOnceMessageV2" || name == "viewOnceMessageV2Extension" {
			omit = true
			return false
		}
		if name == "viewOnce" && f.Kind() == protoreflect.BoolKind && v.Bool() {
			omit = true
			return false
		}
		if (name == "expiration" || name == "ephemeralExpiration") && f.Kind() == protoreflect.Uint32Kind && v.Uint() > 0 {
			omit = true
			return false
		}
		if f.Kind() == protoreflect.MessageKind && !f.IsMap() {
			if f.IsList() {
				list := v.List()
				for i := 0; i < list.Len(); i++ {
					if privateContent(list.Get(i).Message(), depth+1) {
						omit = true
						return false
					}
				}
			} else if privateContent(v.Message(), depth+1) {
				omit = true
				return false
			}
		}
		return true
	})
	return omit
}

func extract(evt *events.Message, source string, omitChat bool) (message, bool) {
	e := *evt
	if e.RawMessage != nil {
		e.UnwrapRaw()
	}
	if e.Message == nil || (e.Info.Chat.Server != types.DefaultUserServer && e.Info.Chat.Server != types.HiddenUserServer && e.Info.Chat.Server != types.GroupServer) {
		return message{}, false
	}
	m := message{ChatID: e.Info.Chat.ToNonAD().String(), ID: e.Info.ID, Sender: e.Info.Sender.ToNonAD().String(),
		SenderName: e.Info.PushName, FromMe: e.Info.IsFromMe, SentAt: e.Info.Timestamp.Unix(),
		Kind: "unsupported", State: "active", Edited: e.IsEdit, Source: source, version: e.Info.Timestamp.UnixMilli()}
	if p := e.Message.GetProtocolMessage(); p != nil {
		switch p.GetType() {
		case waE2E.ProtocolMessage_REVOKE:
			m.ID = p.GetKey().GetID()
			m.Kind = "deleted"
			m.State = "deleted"
			return m, m.ID != ""
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			m.ID = p.GetKey().GetID()
			m.Edited = true
			if p.GetTimestampMS() > 0 {
				m.version = p.GetTimestampMS()
			}
			e.Message = p.GetEditedMessage()
		default:
			return message{}, false
		}
	}
	if e.Message == nil || m.ID == "" {
		return message{}, false
	}
	if omitChat || e.IsEphemeral || e.IsViewOnce || privateContent(e.Message.ProtoReflect(), 0) {
		m.Kind = "ephemeral_or_view_once"
		m.State = "omitted"
		return m, true
	}
	switch {
	case e.Message.Conversation != nil:
		m.Kind = "text"
		m.Text = e.Message.GetConversation()
	case e.Message.ExtendedTextMessage != nil:
		m.Kind = "text"
		m.Text = e.Message.GetExtendedTextMessage().GetText()
	case e.Message.ImageMessage != nil:
		m.Kind = "image"
		m.Text = e.Message.GetImageMessage().GetCaption()
	case e.Message.VideoMessage != nil:
		m.Kind = "video"
		m.Text = e.Message.GetVideoMessage().GetCaption()
	case e.Message.DocumentMessage != nil:
		m.Kind = "document"
		m.Text = e.Message.GetDocumentMessage().GetCaption()
	case e.Message.AudioMessage != nil:
		m.Kind = "audio"
	case e.Message.StickerMessage != nil:
		m.Kind = "sticker"
	case e.Message.ReactionMessage != nil:
		m.Kind = "reaction"
		m.Text = e.Message.GetReactionMessage().GetText()
	}
	return m, true
}

func (r *receiver) failed(stage indexErrorStage) {
	r.errors.Add(1)
	r.errorsByStage[stage].Add(1)
	// Never log message bodies, contacts, raw events, QR contents or session keys.
	log.Printf("WhatsApp indexing failed at %s; status reports incomplete coverage", indexErrorStageNames[stage])
}

func (r *receiver) errorCounts() map[string]int64 {
	counts := make(map[string]int64, len(indexErrorStageNames))
	for stage, name := range indexErrorStageNames {
		counts[name] = r.errorsByStage[stage].Load()
	}
	return counts
}

func (r *receiver) account(ctx context.Context) bool {
	if r.client.Store.ID == nil {
		return false
	}
	if err := r.index.bindAccount(ctx, r.client.Store.ID.ToNonAD().String()); err != nil {
		select {
		case r.fatal <- fmt.Errorf("account binding failed: %w", err):
		default:
		}
		return false
	}
	return true
}

func (r *receiver) record(ctx context.Context, evt *events.Message, source string, omit bool) {
	// Recent clients wrap edits in an additional message-secret encryption
	// envelope. Decrypt locally before the ordinary edit extractor. Work on a
	// copy so other Whatsmeow handlers retain the original event.
	e := *evt
	if e.RawMessage != nil {
		e.UnwrapRaw()
	}
	if enc := e.Message.GetSecretEncryptedMessage(); enc != nil && enc.GetSecretEncType() == waE2E.SecretEncryptedMessage_MESSAGE_EDIT {
		if enc.GetTargetMessageKey().GetID() == "" {
			r.failed(encryptedEditDecrypt)
			return
		}
		if !omit && !e.IsEphemeral && !e.IsViewOnce && !privateContent(e.Message.ProtoReflect(), 0) {
			body, err := r.client.DecryptSecretEncryptedMessage(ctx, &e)
			if err != nil {
				r.failed(encryptedEditDecrypt)
				return
			}
			e.RawMessage = body
			e.Message = body
		}
		e.Info.ID = enc.GetTargetMessageKey().GetID()
		e.IsEdit = true
	}
	m, ok := extract(&e, source, omit)
	if !ok {
		return
	}
	r.normalizeSentMessage(ctx, &m)
	if err := r.index.put(ctx, m); err != nil {
		r.failed(messageStore)
		return
	}
	if err := r.index.confirmSentMessage(ctx, m); err != nil {
		r.failed(messageStore)
	}
	r.lastReceived.Store(time.Now().Unix())
	if !evt.Info.IsGroup {
		fallback := ""
		if !evt.Info.IsFromMe {
			fallback = evt.Info.PushName
		}
		name := r.contactName(ctx, r.contactAliases(ctx, evt.Info.Chat), fallback)
		if err := r.index.renameChat(ctx, m.ChatID, name); err != nil {
			r.failed(chatStore)
		}
	}
}

func (r *receiver) event(evt any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.stopped {
		return
	}
	ctx := r.ctx
	if ctx == nil {
		ctx = context.Background()
	}
	if ctx.Err() != nil {
		return
	}
	switch e := evt.(type) {
	case *events.Connected:
		if r.account(ctx) {
			r.refreshContactNames(ctx)
		}
	case *events.AppStateSyncComplete:
		if e.Name == appstate.WAPatchCriticalUnblockLow && r.account(ctx) {
			r.refreshContactNames(ctx)
		}
	case *events.Contact:
		if r.account(ctx) {
			r.refreshContact(ctx, e.JID)
		}
	case *events.PushName:
		if r.account(ctx) {
			r.refreshContact(ctx, e.JID)
		}
	case *events.BusinessName:
		if r.account(ctx) {
			r.refreshContact(ctx, e.JID)
		}
	case *events.Message:
		if r.account(ctx) {
			r.record(ctx, e, "live", false)
		}
	case *events.HistorySync:
		if !r.account(ctx) {
			return
		}
		for _, c := range e.Data.GetConversations() {
			if ctx.Err() != nil {
				return
			}
			jid, err := types.ParseJID(c.GetID())
			if err != nil {
				r.failed(historyChatParse)
				continue
			}
			if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer && jid.Server != types.GroupServer {
				continue
			}
			name := c.GetName()
			if jid.Server != types.GroupServer {
				name = r.contactName(ctx, r.contactAliases(ctx, jid), name)
			}
			if err = r.index.nameChat(ctx, jid.ToNonAD().String(), name); err != nil {
				r.failed(chatStore)
			}
			for _, item := range c.GetMessages() {
				if ctx.Err() != nil {
					return
				}
				webMsg := item.GetMessage()
				// History includes metadata-only stubs without a message body or
				// sender. They contain no content for this index and must not be
				// reported as failed message imports by the sender parser.
				if webMsg.GetMessage() == nil {
					continue
				}
				msg, err := r.client.ParseWebMessage(jid, webMsg)
				if err != nil {
					r.failed(historyMessageParse)
					continue
				}
				r.record(ctx, msg, "history", c.GetEphemeralExpiration() > 0)
			}
		}
		r.historyBatches.Add(1)
	case *events.LoggedOut:
		select {
		case r.fatal <- fmt.Errorf("WhatsApp unlinked this device; operator pairing is required"):
		default:
		}
	}
}
