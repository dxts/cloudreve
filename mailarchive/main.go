// Command mailarchive incrementally mirrors a read-only IMAP mailbox into an
// m2dir-format mail store on disk. It is a standalone spike: a separate Go
// module that never links into the live Cloudreve server.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
)

func main() {
	addr := flag.String("addr", "imap.gmail.com:993", "IMAP server address (host:port, TLS)")
	mailbox := flag.String("mailbox", "INBOX", `mailbox to archive (e.g. "INBOX" or "[Gmail]/All Mail")`)
	delim := flag.String("delim", "/", "remote hierarchy delimiter, for the on-disk folder layout")
	store := flag.String("store", "./mail", "m2store root directory")
	flag.Parse()

	user := os.Getenv("IMAP_USER")
	pass := os.Getenv("IMAP_PASS")
	if user == "" || pass == "" {
		log.Fatal("set IMAP_USER and IMAP_PASS (for Gmail, an app password)")
	}

	if err := run(*addr, user, pass, *mailbox, *delim, *store); err != nil {
		log.Fatal(err)
	}
}

func run(addr, user, pass, mailbox, delim, storeRoot string) error {
	ctx := context.Background()

	m2, err := OpenM2store(storeRoot)
	if err != nil {
		return fmt.Errorf("open m2store: %w", err)
	}

	st, err := loadState(storeRoot)
	if err != nil {
		return fmt.Errorf("load state: %w", err)
	}

	src, err := DialIMAP(addr, user, pass)
	if err != nil {
		return err
	}
	defer src.Close()

	msgs, newState, err := src.Fetch(ctx, mailbox, st.Mailboxes[mailbox])
	if err != nil {
		return err
	}

	written, present := 0, 0
	for _, m := range msgs {
		ok, err := m2.Store(mailbox, delim, m)
		if err != nil {
			return fmt.Errorf("store UID %d: %w", m.UID, err)
		}
		if ok {
			written++
		} else {
			present++
		}
	}

	st.Mailboxes[mailbox] = newState
	if err := saveState(storeRoot, st); err != nil {
		return fmt.Errorf("save state: %w", err)
	}

	log.Printf("mailbox %q: %d fetched, %d written, %d already present; cursor UIDVALIDITY=%d LastUID=%d",
		mailbox, len(msgs), written, present, newState.UIDValidity, newState.LastUID)
	return nil
}
