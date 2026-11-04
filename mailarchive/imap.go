package main

import (
	"context"
	"fmt"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
)

// IMAPSource is a read-only IMAP backend (tested against Gmail). Gmail lacks
// QRESYNC, so incremental sync is UID-based: new messages only. Flag changes
// and deletions on already-archived messages are out of scope for this spike.
type IMAPSource struct {
	c *imapclient.Client
}

// DialIMAP connects over TLS and logs in. For Gmail, password must be an app
// password (account with 2FA) rather than the normal account password.
func DialIMAP(addr, username, password string) (*IMAPSource, error) {
	c, err := imapclient.DialTLS(addr, nil)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", addr, err)
	}
	if err := c.Login(username, password).Wait(); err != nil {
		_ = c.Close()
		return nil, fmt.Errorf("login as %s: %w", username, err)
	}
	return &IMAPSource{c: c}, nil
}

func (s *IMAPSource) Close() error {
	_ = s.c.Logout().Wait()
	return s.c.Close()
}

func (s *IMAPSource) Fetch(ctx context.Context, mailbox string, state MailboxState) ([]Message, MailboxState, error) {
	// EXAMINE (read-only select): the server must not set \Seen on fetch.
	sel, err := s.c.Select(mailbox, &imap.SelectOptions{ReadOnly: true}).Wait()
	if err != nil {
		return nil, state, fmt.Errorf("select %q: %w", mailbox, err)
	}

	prevLast := state.LastUID
	if sel.UIDValidity != state.UIDValidity {
		prevLast = 0 // UIDVALIDITY rolled: every stored UID is meaningless now
	}
	if sel.NumMessages == 0 {
		return nil, MailboxState{UIDValidity: sel.UIDValidity, LastUID: prevLast}, nil
	}

	var set imap.UIDSet
	set.AddRange(imap.UID(prevLast+1), 0) // 0 == "*", the highest UID

	body := &imap.FetchItemBodySection{Peek: true} // BODY.PEEK[] — never touches \Seen
	opts := &imap.FetchOptions{
		UID:          true,
		Flags:        true,
		Envelope:     true,
		InternalDate: true,
		BodySection:  []*imap.FetchItemBodySection{body},
	}

	cmd := s.c.Fetch(set, opts)
	defer cmd.Close()

	var msgs []Message
	newLast := prevLast
	for {
		md := cmd.Next()
		if md == nil {
			break
		}
		buf, err := md.Collect()
		if err != nil {
			return nil, state, fmt.Errorf("collect message: %w", err)
		}
		uid := uint32(buf.UID)
		// "N:*" returns the single highest message even when N > max, so drop
		// anything at or below the cursor we already hold.
		if uid <= prevLast {
			continue
		}
		raw := buf.FindBodySection(&imap.FetchItemBodySection{})
		if raw == nil {
			return nil, state, fmt.Errorf("message UID %d: no body section returned", uid)
		}
		msgs = append(msgs, Message{
			UID:          uid,
			Flags:        flagStrings(buf.Flags),
			InternalDate: buf.InternalDate,
			From:         envelopeFrom(buf.Envelope),
			Raw:          append([]byte(nil), raw...),
		})
		if uid > newLast {
			newLast = uid
		}
	}

	return msgs, MailboxState{UIDValidity: sel.UIDValidity, LastUID: newLast}, nil
}

func flagStrings(flags []imap.Flag) []string {
	out := make([]string, 0, len(flags))
	for _, f := range flags {
		out = append(out, string(f))
	}
	return out
}

func envelopeFrom(env *imap.Envelope) string {
	if env == nil || len(env.From) == 0 {
		return ""
	}
	a := env.From[0]
	if a.Mailbox != "" && a.Host != "" {
		return a.Mailbox + "@" + a.Host
	}
	return a.Name
}
