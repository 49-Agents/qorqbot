package main

import (
	"context"

	"go.mau.fi/whatsmeow/types"
)

// These lookups only read the linked device's local stores. They never fetch
// contacts from WhatsApp, open a chat or change any read state. IDs remain
// separate in the message index even when they share a display name.
func (r *receiver) contactAliases(ctx context.Context, jid types.JID) []types.JID {
	jid = jid.ToNonAD()
	if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer {
		return nil
	}
	aliases := []types.JID{jid}
	if r.client != nil && r.client.Store != nil && r.client.Store.LIDs != nil {
		alt, err := r.client.Store.GetAltJID(ctx, jid)
		if err != nil {
			r.failed(contactLookup)
			return nil
		}
		if !alt.IsEmpty() && alt.ToNonAD() != jid {
			// Prefer the phone-address contact if both aliases have names.
			if jid.Server == types.HiddenUserServer {
				aliases = []types.JID{alt.ToNonAD(), jid}
			} else {
				aliases = append(aliases, alt.ToNonAD())
			}
		}
	}
	return aliases
}

func (r *receiver) contactName(ctx context.Context, aliases []types.JID, fallback string) string {
	if len(aliases) == 0 {
		return ""
	}
	if r.client == nil || r.client.Store == nil || r.client.Store.Contacts == nil {
		return fallback
	}
	var first, push, business string
	for _, jid := range aliases {
		contact, err := r.client.Store.Contacts.GetContact(ctx, jid)
		if err != nil {
			r.failed(contactLookup)
			return ""
		}
		if contact.FullName != "" {
			return contact.FullName
		}
		if first == "" {
			first = contact.FirstName
		}
		if push == "" {
			push = contact.PushName
		}
		if business == "" {
			business = contact.BusinessName
		}
	}
	for _, name := range []string{first, push, business, fallback} {
		if name != "" {
			return name
		}
	}
	return ""
}

func (r *receiver) refreshContact(ctx context.Context, jid types.JID) {
	aliases := r.contactAliases(ctx, jid)
	name := r.contactName(ctx, aliases, "")
	if name == "" {
		return
	}
	for _, alias := range aliases {
		if err := r.index.renameChat(ctx, alias.String(), name); err != nil {
			r.failed(chatStore)
		}
	}
}

func (r *receiver) refreshContactNames(ctx context.Context) {
	// Read and close each page before writing: the index has one connection.
	// This also repairs names in an existing index without re-pairing/history.
	for after := ""; ; {
		page, err := r.index.chats(ctx, "", after, 100)
		if err != nil {
			r.failed(chatStore)
			return
		}
		for _, chat := range page.Chats {
			if ctx.Err() != nil {
				return
			}
			jid, err := types.ParseJID(chat.ID)
			if err == nil {
				r.refreshContact(ctx, jid)
			}
		}
		if page.Next == "" {
			return
		}
		after = page.Next
	}
}
