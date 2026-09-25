package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// Protocol (backend /v1/keyboard):
//
//	client -> {"type":"hello","client":"<name>","room":"voice-keyboard"}
//	server -> {"type":"ready","credential":"…"}              credential replaces an install token
//	server -> {"type":"segment","text":"…"}                  type verbatim
//	server -> {"type":"key","key":"enter","state":"press"}   press|down|up|hold
type message struct {
	Type       string `json:"type"`
	Text       string `json:"text"`
	Key        string `json:"key"`
	State      string `json:"state"`
	Credential string `json:"credential"`
	Room       string `json:"room"`
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
func connect(ctx context.Context, server, token, client, room string) (*websocket.Conn, message, error) {
	address, err := keyboardURL(server, token)
	if err != nil {
		return nil, message{}, err
	}
	conn, _, err := websocket.Dial(ctx, address, nil)
	if err != nil {
		return nil, message{}, err
	}
	conn.SetReadLimit(1 << 20)
	hello, _ := json.Marshal(struct {
		Type   string `json:"type"`
		Client string `json:"client"`
		Room   string `json:"room"`
	}{"hello", client, room})
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
	conn, ready, err := connect(ctx, server, token, name, "voice-keyboard")
	if websocket.CloseStatus(err) == closeBadCredential {
		return config{}, errors.New("the install command was replaced; copy a fresh one from Settings → Computers")
	}
	if err != nil {
		return config{}, fmt.Errorf("could not reach %s: %w", server, err)
	}
	conn.Close(websocket.StatusNormalClosure, "")
	return config{Server: server, Client: name, Room: "voice-keyboard", Credential: ready.Credential}, nil
}

// serve keeps a connection open and types what arrives, reconnecting with
// back-off until ctx is cancelled.
func serve(ctx context.Context, cfg config) error {
	kb, err := newKeyboard()
	if err != nil {
		return err
	}
	defer kb.Close()
	log.Printf("voice-keyboard %s starting as %q (%s)", version, cfg.Client, typingBackendName())
	go watchPermission(ctx)
	delay := time.Second
	for {
		updateState(state{PID: pidSelf(), Since: time.Now()})
		err := session(ctx, cfg, kb)
		if ctx.Err() != nil {
			return nil
		}
		reason := err.Error()
		if websocket.CloseStatus(err) == closeBadCredential {
			reason = "the server rejected this computer; run the install command again"
			delay = time.Minute
		}
		log.Printf("disconnected: %s; retrying in %s", reason, delay)
		updateState(state{PID: pidSelf(), Since: time.Now(), LastError: reason})
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(delay):
		}
		delay = min(delay*2, 30*time.Second)
	}
}

func session(ctx context.Context, cfg config, kb keyboard) error {
	dialCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	conn, _, err := connect(dialCtx, cfg.Server, cfg.Credential, cfg.Client, cfg.Room)
	cancel()
	if err != nil {
		return err
	}
	defer conn.CloseNow()
	log.Printf("connected to %s", cfg.Server)
	updateState(state{PID: pidSelf(), Connected: true, Since: time.Now()})

	// Pings detect a dead connection (sleep, network change) within ~40 s.
	pingCtx, stopPing := context.WithCancel(ctx)
	defer stopPing()
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
			if err := kb.Type(m.Text); err != nil {
				log.Printf("typing failed: %v", err)
			}
		case "key":
			if err := kb.Key(m.Key, m.State); err != nil {
				log.Printf("key %s failed: %v", m.Key, err)
			}
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
		warning = ""
		if err != nil {
			warning = err.Error()
		}
		s := current
		stateMu.Unlock()
		if s.PID != 0 {
			updateState(s)
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

func readJSON(ctx context.Context, conn *websocket.Conn, v any) error {
	_, data, err := conn.Read(ctx)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}
