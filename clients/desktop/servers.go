package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"log"
	"net"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"
)

// A deployment can have several addresses (VK_URLS on the server), e.g. its
// LAN address and a Cloudflare name. The server lists them in "ready", with
// its instance id and local CA. Each connection goes to the fastest address
// that answers with the same instance id, local addresses first; while on a
// public address, local ones are re-checked every 15 seconds (back home, the
// client moves to the LAN within seconds).

const localCheckEvery = 15 * time.Second

// errSwitching ends a session on purpose, to reconnect to a local address.
var errSwitching = errors.New("switching to a local address")

var (
	clientsMu sync.Mutex
	clientsCA string
	clients   map[bool]*http.Client // by isLocal
)

// httpClient trusts the system's CAs plus the deployment's local CA, if any.
// For a local address the certificate need not name it (an IP missing from
// VK_DOMAIN), but it must still come from a trusted CA: the deployment's CA
// arrives over an already verified connection, so another device on the LAN
// cannot pose as the server. No overall timeout: it also carries the WebSocket.
func httpClient(ca, server string) *http.Client {
	local := isLocal(server)
	clientsMu.Lock()
	defer clientsMu.Unlock()
	if clients != nil && clientsCA == ca {
		return clients[local]
	}
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if ca != "" {
		pool.AppendCertsFromPEM([]byte(ca))
	}
	newClient := func(config *tls.Config) *http.Client {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.TLSClientConfig = config
		return &http.Client{Transport: transport}
	}
	clientsCA = ca
	clients = map[bool]*http.Client{
		false: newClient(&tls.Config{RootCAs: pool}),
		true: newClient(&tls.Config{RootCAs: pool, InsecureSkipVerify: true, // verified below, without the name
			VerifyConnection: func(cs tls.ConnectionState) error {
				if len(cs.PeerCertificates) == 0 {
					return errors.New("no certificate")
				}
				intermediates := x509.NewCertPool()
				for _, c := range cs.PeerCertificates[1:] {
					intermediates.AddCert(c)
				}
				_, err := cs.PeerCertificates[0].Verify(x509.VerifyOptions{Roots: pool, Intermediates: intermediates})
				return err
			}}),
	}
	return clients[local]
}

// isLocal: a private or loopback IP, a .local name, or a single-label name.
func isLocal(server string) bool {
	u, err := url.Parse(server)
	if err != nil {
		return false
	}
	host := u.Hostname()
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast()
	}
	return host == "localhost" || strings.HasSuffix(host, ".local") || !strings.Contains(host, ".")
}

// candidates: the configured address and every address the server listed.
func candidates(cfg config) []string {
	list := []string{cfg.Server}
	for _, s := range cfg.Servers {
		if s = strings.TrimRight(s, "/"); s != "" && !slices.Contains(list, s) {
			list = append(list, s)
		}
	}
	return list
}

// probe fetches /api/instance: the round trip, or ok=false if the address is
// unreachable or leads to another deployment.
func probe(ctx context.Context, cfg config, server string) (time.Duration, bool) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, server+"/api/instance", nil)
	if err != nil {
		return 0, false
	}
	started := time.Now()
	resp, err := httpClient(cfg.CA, server).Do(req)
	if err != nil {
		return 0, false
	}
	defer resp.Body.Close()
	var body struct {
		Instance string `json:"instance"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&body) != nil || body.Instance != cfg.Instance {
		return 0, false
	}
	return time.Since(started), true
}

// rankServers orders the addresses to try: verified local ones, then verified
// public ones, each fastest first, then the rest (a server restarting may not
// answer the probe, but the configured address is always worth a try).
func rankServers(ctx context.Context, cfg config) []string {
	list := candidates(cfg)
	if len(list) == 1 || cfg.Instance == "" {
		return list
	}
	type result struct {
		server string
		rtt    time.Duration
		ok     bool
	}
	results := make([]result, len(list))
	var wg sync.WaitGroup
	for i, s := range list {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rtt, ok := probe(ctx, cfg, s)
			results[i] = result{s, rtt, ok}
		}()
	}
	wg.Wait()
	sort.SliceStable(results, func(i, j int) bool {
		a, b := results[i], results[j]
		if a.ok != b.ok {
			return a.ok
		}
		if !a.ok {
			return false // unverified: keep the configured order
		}
		if la, lb := isLocal(a.server), isLocal(b.server); la != lb {
			return la
		}
		return a.rtt < b.rtt
	})
	ranked := make([]string, 0, len(results))
	for _, r := range results {
		if r.ok || r.server == cfg.Server {
			ranked = append(ranked, r.server)
		}
	}
	return ranked
}

// betterLocal reports a verified local address while connected to a public one.
func betterLocal(ctx context.Context, cfg config, active string) string {
	if isLocal(active) || cfg.Instance == "" {
		return ""
	}
	for _, s := range candidates(cfg) {
		if s != active && isLocal(s) {
			if _, ok := probe(ctx, cfg, s); ok {
				return s
			}
		}
	}
	return ""
}

// learnServers keeps what "ready" says about the deployment's addresses.
// It reports whether anything changed.
func learnServers(cfg *config, ready message) bool {
	if ready.Instance == "" {
		return false // an older server
	}
	servers := make([]string, 0, len(ready.URLs))
	for _, s := range ready.URLs {
		if u, err := url.Parse(s); err == nil && (u.Scheme == "https" || u.Scheme == "http") && u.Host != "" {
			servers = append(servers, strings.TrimRight(s, "/"))
		}
	}
	if cfg.Instance == ready.Instance && cfg.CA == ready.CA && slices.Equal(cfg.Servers, servers) {
		return false
	}
	cfg.Instance, cfg.CA, cfg.Servers = ready.Instance, ready.CA, servers
	log.Printf("addresses of this server: %s", strings.Join(candidates(*cfg), ", "))
	return true
}
