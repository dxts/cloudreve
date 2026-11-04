package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// syncState is the per-mailbox cursor set. It lives as a dotfile in the
// m2store root, which m2dir readers ignore (they only look at folders and
// markers), keeping sync bookkeeping out of band from the mail itself.
type syncState struct {
	Mailboxes map[string]MailboxState `json:"mailboxes"`
}

func statePath(root string) string { return filepath.Join(root, ".sync-state.json") }

func loadState(root string) (*syncState, error) {
	st := &syncState{Mailboxes: map[string]MailboxState{}}
	data, err := os.ReadFile(statePath(root))
	if os.IsNotExist(err) {
		return st, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, st); err != nil {
		return nil, err
	}
	if st.Mailboxes == nil {
		st.Mailboxes = map[string]MailboxState{}
	}
	return st, nil
}

func saveState(root string, st *syncState) error {
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	tmp := statePath(root) + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, statePath(root))
}
