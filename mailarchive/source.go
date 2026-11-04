package main

import (
	"context"
	"time"
)

// Message is one fetched email: its IMAP coordinates, flags, and the raw
// RFC822 bytes exactly as the server returned them.
type Message struct {
	UID          uint32
	Flags        []string
	InternalDate time.Time
	From         string
	Raw          []byte
}

// MailboxState is the incremental-sync cursor for a single mailbox. A changed
// UIDVALIDITY invalidates LastUID and forces a full re-pull.
type MailboxState struct {
	UIDValidity uint32 `json:"uidvalidity"`
	LastUID     uint32 `json:"last_uid"`
}

// MailSource is a read-only remote mail store we can incrementally pull from.
// It mirrors the MailSource abstraction in the basement design doc; IMAP is the
// first backend, JMAP the planned second.
type MailSource interface {
	// Fetch returns every message in mailbox with a UID greater than the one
	// recorded in state, together with the advanced cursor.
	Fetch(ctx context.Context, mailbox string, state MailboxState) ([]Message, MailboxState, error)
	Close() error
}
