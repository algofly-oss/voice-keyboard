package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/url"
	"runtime"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/coder/websocket"
)

// Protocol (backend /v1/keyboard):
//
//	client -> {"type":"hello","client":"<name>","machine":"…","platform":"…","version":"…"}
//	server -> {"type":"ready","credential":"…"}              credential replaces an install token
//	server -> {"type":"segment","text":"…"}                  type verbatim
//	server -> {"type":"key","key":"enter","state":"press"}   press|down|up|hold
type message struct {
	Type       string `json:"type"`
	Text       string `json:"text"`
	Key        string `json:"key"`
	State      string `json:"state"`
	Credential string `json:"credential"`
	Selected   bool   `json:"selected"`
	// type "pointer", from the web app's touchpad
	Action string `json:"action"` // move, click, scroll
	DX     int    `json:"dx"`
	DY     int    `json:"dy"`
	Button string `json:"button"`
}

const closeBadCredential = 4401

func keyboardURL(server, token string) (string, error) {
	u, err := url.Parse(strings.TrimRight(server, "/") + "/v1/keyboard")
	if err != nil {
		return "", err
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		u.Scheme = "ws"
	default:
		return "", fmt.Errorf("server URL must start with http:// or https://")
	}
	u.RawQuery = url.Values{"token": {token}}.Encode()
	return u.String(), nil
}

// connect opens the socket and completes the hello/ready handshake.
func connect(ctx context.Context, server, token, client string) (*websocket.Conn, message, error) {
	address, err := keyboardURL(server, token)
	if err != nil {
		return nil, message{}, err
	}
	conn, _, err := websocket.Dial(ctx, address, nil)
	if err != nil {
		return nil, message{}, err
	}
	conn.SetReadLimit(1 << 20)
	// The server shows these in the web app so the user can tell computers apart.
	hello, _ := json.Marshal(struct {
		Type     string `json:"type"`
		Client   string `json:"client"`
		Machine  string `json:"machine"`
		Platform string `json:"platform"`
		Version  string `json:"version"`
	}{"hello", client, machineID(), runtime.GOOS + "/" + runtime.GOARCH, version})
	if err := conn.Write(ctx, websocket.MessageText, hello); err != nil {
		conn.CloseNow()
		return nil, message{}, err
	}
	var ready message
	if err := readJSON(ctx, conn, &ready); err != nil {
		conn.CloseNow()
		return nil, message{}, err
	}
	if ready.Type != "ready" {
		conn.CloseNow()
		return nil, message{}, fmt.Errorf("unexpected reply %q", ready.Type)
	}
	return conn, ready, nil
}

func enroll(ctx context.Context, server, token, name string) (config, error) {
	conn, ready, err := connect(ctx, server, token, name)
	if websocket.CloseStatus(err) == closeBadCredential {
		return config{}, errors.New("the install command was replaced; copy a fresh one from Settings → Clients")
	}
	if err != nil {
		return config{}, fmt.Errorf("could not reach %s: %w", server, err)
	}
	conn.Close(websocket.StatusNormalClosure, "")
	return config{Server: server, Client: name, Credential: ready.Credential}, nil
}

// serve keeps a connection open and types what arrives. It never gives up:
// every failure (server down, network change, bad credential, typing backend
// not ready yet at login, even a panic) is logged and retried with back-off
// until ctx is cancelled by stop/logout.
func serve(ctx context.Context, cfg config) error {
	log.Printf("vkeyboard %s starting as %q (%s)", version, cfg.Client, typingBackendName())
	go watchPermission(ctx)
	kb := waitForKeyboard(ctx, cfg)
	if kb == nil {
		return nil
	}
	defer kb.Close()
	log.Printf("typing method: %s", typingDescription(cfg))
	delay := time.Second
	for {
		updateState(state{PID: pidSelf(), Since: time.Now()})
		began := time.Now()
		err := safeSession(ctx, cfg, kb)
		if connectedAt.After(began) {
			delay = time.Second // it was connected: a drop is retried at once, not after the old back-off
		}
		if ctx.Err() != nil {
			return nil
		}
		reason := err.Error()
		if websocket.CloseStatus(err) == closeBadCredential {
			reason = "the server rejected this client (removed?); run the install command again"
			delay = time.Minute
		}
		log.Printf("disconnected: %s; retrying in %s", reason, delay)
		updateState(state{PID: pidSelf(), Since: time.Now(), LastError: reason})
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
		delay = min(delay*2, 10*time.Second)
	}
}

// connectedAt is when the last session finished its handshake.
var connectedAt time.Time

// Errors go to the local log and to the server, which keeps them per client
// (GET /api/client-errors), so failures on any computer can be looked up in
// one place. Reported while offline, they wait for the next connection.
var errorReports = make(chan string, 50)

func reportError(format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	log.Print(msg)
	select {
	case errorReports <- msg:
	default: // the queue is full while offline; the local log still has it
	}
}

func sendErrorReports(ctx context.Context, conn *websocket.Conn) {
	for {
		select {
		case <-ctx.Done():
			return
		case msg := <-errorReports:
			b, _ := json.Marshal(struct {
				Type    string `json:"type"`
				Level   string `json:"level"`
				Message string `json:"message"`
			}{"log", "error", version + " " + runtime.GOOS + "/" + runtime.GOARCH + ": " + msg})
			if err := conn.Write(ctx, websocket.MessageText, b); err != nil {
				select { // not delivered: keep it for the next connection
				case errorReports <- msg:
				default:
				}
				return
			}
		}
	}
}

// waitForKeyboard retries until typing is possible; at login the display
// server may not be ready when the login item starts.
func waitForKeyboard(ctx context.Context, cfg config) keyboard {
	for delay := time.Second; ; delay = min(delay*2, 30*time.Second) {
		kb, err := newConfiguredKeyboard(cfg)
		if err == nil {
			return kb
		}
		reportError("cannot type yet: %v; retrying in %s", err, delay)
		updateState(state{PID: pidSelf(), Since: time.Now(), LastError: "cannot type yet: " + err.Error()})
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
	}
}

// safeSession turns a panic inside a session into an error so serve retries.
func safeSession(ctx context.Context, cfg config, kb keyboard) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("internal error: %v", r)
		}
	}()
	return session(ctx, cfg, kb)
}

func session(ctx context.Context, cfg config, kb keyboard) error {
	dialCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	conn, ready, err := connect(dialCtx, cfg.Server, cfg.Credential, cfg.Client)
	cancel()
	if err != nil {
		return err
	}
	defer conn.CloseNow()
	if ready.Credential != "" && ready.Credential != cfg.Credential {
		// The server upgraded an older credential; keep the new one.
		cfg.Credential = ready.Credential
		if err := saveConfig(cfg); err != nil {
			reportError("could not save the new credential: %v", err)
		}
	}
	selected := ready.Selected
	connectedAt = time.Now()
	log.Printf("connected to %s (%s)", cfg.Server, selectedText(selected))
	updateState(state{PID: pidSelf(), Connected: true, Since: time.Now(), Selected: &selected})

	// Pings detect a dead connection (sleep, network change) within ~40 s.
	pingCtx, stopPing := context.WithCancel(ctx)
	defer stopPing()
	go sendErrorReports(pingCtx, conn)
	go func() {
		for {
			select {
			case <-pingCtx.Done():
				return
			case <-time.After(20 * time.Second):
				c, cancel := context.WithTimeout(pingCtx, 20*time.Second)
				err := conn.Ping(c)
				cancel()
				if err != nil && pingCtx.Err() == nil {
					conn.Close(websocket.StatusGoingAway, "ping timeout")
					return
				}
			}
		}
	}()

	for {
		var m message
		if err := readJSON(ctx, conn, &m); err != nil {
			return err
		}
		switch m.Type {
		case "segment":
			// Only lengths are logged: dictated text can contain passwords.
			chars, started := utf8.RuneCountInString(m.Text), time.Now()
			log.Printf("received %d characters", chars)
			if err := kb.Type(m.Text); err != nil {
				reportError("typing failed: %v", err)
			} else {
				log.Printf("typed %d characters in %s", chars, time.Since(started).Round(time.Millisecond))
			}
		case "key":
			if err := kb.Key(m.Key, m.State); err != nil {
				reportError("key %s failed: %v", m.Key, err)
			} else if m.State == "" || m.State == "press" || m.State == "down" {
				log.Printf("pressed %s", m.Key)
			}
		case "pointer":
			handlePointer(m)
		case "selected":
			selected := m.Selected
			log.Print(selectedText(selected))
			updateState(state{PID: pidSelf(), Connected: true, Since: readState().Since, Selected: &selected})
		}
	}
}

var (
	stateMu sync.Mutex
	warning string
)

// updateState records the connection state, keeping the latest permission warning.
func updateState(s state) {
	stateMu.Lock()
	defer stateMu.Unlock()
	s.Warning = warning
	current = s
	writeState(s)
}

var current state

// watchPermission asks for the typing permission once (macOS shows its dialog
// for this agent) and re-checks until it is granted, so typing starts without
// a restart once the user allows it.
func watchPermission(ctx context.Context) {
	prompted := false
	for {
		err := checkTypingPermission(!prompted)
		prompted = true
		stateMu.Lock()
		changed := err != nil && err.Error() != warning
		warning = ""
		if err != nil {
			warning = err.Error()
		}
		s := current
		stateMu.Unlock()
		if s.PID != 0 {
			updateState(s)
		}
		if changed { // e.g. Accessibility not allowed on macOS: typing and the touchpad fail
			reportError("permission: %v", err)
		}
		if err == nil {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}

func selectedText(selected bool) string {
	if selected {
		return "this is the active client; it types dictation"
	}
	return "another client is active; pick this one in Settings → Clients to type here"
}

func readJSON(ctx context.Context, conn *websocket.Conn, v any) error {
	_, data, err := conn.Read(ctx)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}
