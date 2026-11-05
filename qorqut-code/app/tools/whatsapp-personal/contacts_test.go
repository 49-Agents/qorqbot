package main

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type fixtureContacts struct {
	store.ContactStore
	names map[types.JID]types.ContactInfo
	fail  bool
}

func (s *fixtureContacts) GetContact(_ context.Context, jid types.JID) (types.ContactInfo, error) {
	if s.fail {
		return types.ContactInfo{}, errors.New("synthetic contact lookup failure")
	}
	return s.names[jid], nil
}

type fixtureLIDs struct {
	store.LIDStore
	pn, lid types.JID
}

func (s *fixtureLIDs) GetPNForLID(_ context.Context, jid types.JID) (types.JID, error) {
	if jid == s.lid {
		return s.pn, nil
	}
	return types.EmptyJID, nil
}

func (s *fixtureLIDs) GetLIDForPN(_ context.Context, jid types.JID) (types.JID, error) {
	if jid == s.pn {
		return s.lid, nil
	}
	return types.EmptyJID, nil
}

func TestContactNames(t *testing.T) {
	ctx := context.Background()
	s := testIndex(t)
	owner := types.NewJID("owner", types.DefaultUserServer)
	pn := types.NewJID("friend", types.DefaultUserServer)
	lid := types.NewJID("friend-lid", types.HiddenUserServer)
	group := types.NewJID("group", types.GroupServer)
	unseen := types.NewJID("unseen", types.DefaultUserServer)
	contacts := &fixtureContacts{names: map[types.JID]types.ContactInfo{
		pn:  {FullName: "Saved friend", PushName: "Remote nickname"},
		lid: {PushName: "Remote nickname"}, unseen: {FullName: "Other friend"},
	}}
	r := &receiver{ctx: ctx, index: s, fatal: make(chan error, 1), client: whatsmeow.NewClient(&store.Device{
		ID: &owner, Contacts: contacts, LIDs: &fixtureLIDs{pn: pn, lid: lid},
	}, waLog.Noop)}
	for _, jid := range []types.JID{pn, lid, group} {
		if err := s.nameChat(ctx, jid.String(), "Old name"); err != nil {
			t.Fatal(err)
		}
	}
	assertMatches := func(name string, ids ...types.JID) {
		t.Helper()
		page, err := s.chats(ctx, name, "", 100)
		if err != nil || len(page.Chats) != len(ids) {
			t.Fatalf("name lookup %q: %+v %v", name, page, err)
		}
		want := make(map[string]bool)
		for _, jid := range ids {
			want[jid.String()] = true
		}
		for _, chat := range page.Chats {
			if !want[chat.ID] || chat.Name != name {
				t.Fatalf("incorrect name or merged identity: %+v", chat)
			}
		}
	}
	// Reconnecting repairs an already populated index using local contacts.
	r.event(&events.Connected{})
	assertMatches("Saved friend", pn, lid)
	assertMatches("Old name", group)
	r.event(&events.Contact{JID: unseen})
	assertMatches("Other friend") // Contact sync must not create empty chats.

	// History titles and incoming push names cannot replace a saved name.
	r.event(&events.HistorySync{Data: &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{
		ID: proto.String(lid.String()), Name: proto.String("History nickname"),
	}}}})
	r.event(&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: lid, Sender: lid},
		ID: "incoming", Timestamp: time.Unix(100, 0), PushName: "Remote nickname"},
		Message: &waE2E.Message{Conversation: proto.String("synthetic incoming")}})
	assertMatches("Saved friend", pn, lid)
	// An outgoing-only conversation gets the peer's name, never our push name.
	r.event(&events.Message{Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: unseen, Sender: owner, IsFromMe: true},
		ID: "outgoing", Timestamp: time.Unix(101, 0), PushName: "Owner nickname"},
		Message: &waE2E.Message{Conversation: proto.String("synthetic outgoing")}})
	assertMatches("Other friend", unseen)
	assertMatches("Owner nickname")

	contacts.names[pn] = types.ContactInfo{FirstName: "Renamed friend", PushName: "Remote nickname"}
	r.event(&events.Contact{JID: pn})
	assertMatches("Renamed friend", pn, lid)
	r.event(&events.PushName{JID: lid, NewPushName: "Remote nickname"})
	r.event(&events.BusinessName{JID: pn, NewBusinessName: "Business nickname"})
	assertMatches("Renamed friend", pn, lid)
	contacts.names[pn] = types.ContactInfo{FullName: "Resynced friend"}
	r.event(&events.AppStateSyncComplete{Name: appstate.WAPatchCriticalUnblockLow})
	assertMatches("Resynced friend", pn, lid)
	assertMatches("Old name", group)

	contacts.fail = true
	r.event(&events.Contact{JID: lid})
	assertMatches("Resynced friend", pn, lid)
	if r.errors.Load() != 1 || r.errorCounts()["contact_lookup"] != 1 {
		t.Fatalf("contact failure hidden or misclassified: %+v", r.errorCounts())
	}
	page, err := s.changes(ctx, 0, 100)
	if err != nil || len(page.Changes) != 2 {
		t.Fatalf("contact refresh changed the message cursor: %+v %v", page, err)
	}
}
