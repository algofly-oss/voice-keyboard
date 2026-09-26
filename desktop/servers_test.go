package main

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestIsLocal(t *testing.T) {
	for server, want := range map[string]bool{
		"https://192.168.0.3:8272": true, "https://10.1.2.3": true, "http://127.0.0.1:8000": true,
		"https://luna.local": true, "https://luna:443": true, "https://localhost": true,
		"https://vkeyboard.example.com": false, "https://8.8.8.8": false,
	} {
		if got := isLocal(server); got != want {
			t.Errorf("isLocal(%q) = %v, want %v", server, got, want)
		}
	}
}

func instanceServer(id string, delay time.Duration) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(delay)
		w.Write([]byte(`{"instance":"` + id + `"}`))
	}))
}

func TestRankServers(t *testing.T) {
	slow := instanceServer("abc", 50*time.Millisecond)
	fast := instanceServer("abc", 0)
	other := instanceServer("xyz", 0)
	defer slow.Close()
	defer fast.Close()
	defer other.Close()
	// 127.0.0.1 servers stand in for local ones; a localhost name for a second local one.
	fastByName := strings.Replace(fast.URL, "127.0.0.1", "localhost", 1)
	public := "https://public.invalid"
	cfg := config{Server: public, Instance: "abc", Servers: []string{public, slow.URL, other.URL, fastByName}}
	got := rankServers(context.Background(), cfg)
	want := []string{fastByName, slow.URL, public} // verified local by speed; another deployment dropped; configured kept
	if !slices.Equal(got, want) {
		t.Fatalf("rankServers = %v, want %v", got, want)
	}
	if s := betterLocal(context.Background(), cfg, public); s != slow.URL && s != fastByName {
		t.Fatalf("betterLocal = %q", s)
	}
	if s := betterLocal(context.Background(), cfg, slow.URL); s != "" {
		t.Fatalf("betterLocal on a local address = %q", s)
	}
}

// A local address is trusted with the deployment's CA even when the
// certificate does not name it; a public address, or another CA, is not.
func TestLocalCertificateWithoutName(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"instance":"abc"}`))
	}))
	defer srv.Close()
	ca := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}))
	byName := strings.Replace(srv.URL, "127.0.0.1", "localhost", 1) // the certificate names 127.0.0.1, not localhost
	cfg := config{Instance: "abc", CA: ca}
	if _, ok := probe(context.Background(), cfg, byName); !ok {
		t.Fatal("local address with the deployment's CA was refused")
	}
	if _, ok := probe(context.Background(), config{Instance: "abc"}, byName); ok {
		t.Fatal("local address without the CA was accepted")
	}
	if httpClient(ca, "https://public.example.com") == httpClient(ca, byName) {
		t.Fatal("public addresses must check the name")
	}
}
