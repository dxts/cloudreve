package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"hash/fnv"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// imapToM2dirFlag maps common IMAP system flags to the m2dir standard flags
// (IANA keywords, with \Deleted rendered as $Deleted).
var imapToM2dirFlag = map[string]string{
	`\Seen`:     "$seen",
	`\Answered`: "$answered",
	`\Flagged`:  "$flagged",
	`\Deleted`:  "$Deleted",
	`\Draft`:    "$draft",
}

// M2store is an m2dir-spec mail store rooted at Root.
type M2store struct {
	Root string
}

// OpenM2store creates the store root and its empty .m2store discovery marker.
func OpenM2store(root string) (*M2store, error) {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	if err := ensureMarker(filepath.Join(root, ".m2store")); err != nil {
		return nil, err
	}
	return &M2store{Root: root}, nil
}

// Store writes msg into the mailbox's m2dir. It returns false (and writes
// nothing) if a message with byte-identical content is already present, so
// re-runs and full re-pulls stay idempotent.
func (s *M2store) Store(mailbox, delim string, msg Message) (bool, error) {
	dir, err := s.mailboxDir(mailbox, delim)
	if err != nil {
		return false, err
	}

	checksum := m2dirChecksum(msg.Raw)
	if existingChecksums(dir)[checksum] {
		return false, nil
	}
	nonce, err := randomNonce()
	if err != nil {
		return false, err
	}
	id := checksum + "." + nonce
	name := humanPart(msg) + "," + id

	// Write to a temp dotfile (ignored by readers), then rename into place —
	// atomic within the same directory.
	tmp, err := os.CreateTemp(dir, ".m2dir.tmp.*")
	if err != nil {
		return false, err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(msg.Raw); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return false, err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return false, err
	}
	if err := os.Rename(tmpName, filepath.Join(dir, name)); err != nil {
		os.Remove(tmpName)
		return false, err
	}

	// Flags sidecar: .meta/<id>.flags, one m2dir flag per line.
	if flags := mapFlags(msg.Flags); len(flags) > 0 {
		fp := filepath.Join(dir, ".meta", id+".flags")
		if err := os.WriteFile(fp, []byte(strings.Join(flags, "\n")+"\n"), 0o600); err != nil {
			return true, fmt.Errorf("write flags for %s: %w", id, err)
		}
	}
	return true, nil
}

// mailboxDir maps a remote mailbox name (split on delim) to the leaf m2dir
// path, creating the folder chain, its .meta dir and .m2dir marker.
func (s *M2store) mailboxDir(mailbox, delim string) (string, error) {
	dir := s.Root
	for _, p := range strings.Split(mailbox, delim) {
		if p == "" {
			continue
		}
		dir = filepath.Join(dir, encodeFolder(p))
	}
	if err := os.MkdirAll(filepath.Join(dir, ".meta"), 0o700); err != nil {
		return "", err
	}
	if err := ensureMarker(filepath.Join(dir, ".m2dir")); err != nil {
		return "", err
	}
	return dir, nil
}

// existingChecksums returns the set of message checksums already stored in a
// leaf m2dir, parsed from the <checksum>.<nonce> id tail of each filename.
func existingChecksums(dir string) map[string]bool {
	out := map[string]bool{}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		if id := parseID(e.Name()); id != "" {
			if cs, _, ok := strings.Cut(id, "."); ok {
				out[cs] = true
			}
		}
	}
	return out
}

// m2dirChecksum implements the spec's unique-ID checksum: FNV64a over the
// message salted by prepending its little-endian 32-bit size, encoded as
// size(4 LE) ++ hash(8 LE), base64url without padding (16 chars).
func m2dirChecksum(raw []byte) string {
	var sizeLE [4]byte
	binary.LittleEndian.PutUint32(sizeLE[:], uint32(len(raw)))

	h := fnv.New64a()
	h.Write(sizeLE[:]) // salt: prepend the size
	h.Write(raw)

	var out [12]byte
	copy(out[:4], sizeLE[:])
	binary.LittleEndian.PutUint64(out[4:], h.Sum64())
	return base64.RawURLEncoding.EncodeToString(out[:])
}

// randomNonce is 3 random bytes, base64url without padding (4 chars).
func randomNonce() (string, error) {
	var b [3]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b[:]), nil
}

// humanPart builds the human-centric filename prefix: YYYY-MM-DD_hh:mm_<from>.
// It is for human eyes only and must never be parsed by applications.
func humanPart(msg Message) string {
	ts := msg.InternalDate
	if ts.IsZero() {
		ts = time.Unix(0, 0).UTC()
	}
	return ts.UTC().Format("2006-01-02_15:04") + "_" + sanitizeHuman(msg.From)
}

// parseID returns the <checksum>.<nonce> id by searching back for the last
// comma, since the human part may itself contain commas.
func parseID(filename string) string {
	i := strings.LastIndexByte(filename, ',')
	if i < 0 {
		return ""
	}
	return filename[i+1:]
}

func mapFlags(flags []string) []string {
	var out []string
	for _, f := range flags {
		if m, ok := imapToM2dirFlag[f]; ok {
			out = append(out, m)
		} else if strings.HasPrefix(f, "$") {
			out = append(out, f) // already an m2dir/IMAP keyword
		}
	}
	return out
}

// encodeFolder percent-encodes the characters the spec requires (% and the
// path separator), keeping names otherwise legible per the spec's guidance.
func encodeFolder(name string) string {
	name = strings.ReplaceAll(name, "%", "%25")
	name = strings.ReplaceAll(name, "/", "%2F")
	return name
}

func sanitizeHuman(s string) string {
	s = strings.ReplaceAll(s, "/", "_")
	s = strings.ReplaceAll(s, ",", "_")
	s = strings.ReplaceAll(s, " ", "_")
	if s == "" {
		return "unknown"
	}
	return s
}

func ensureMarker(path string) error {
	if _, err := os.Stat(path); err == nil {
		return nil
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	return f.Close()
}
