// Command voice-keyboard types text dictated in the Voice Keyboard web app into
// the focused application on this computer.
//
// It is a single self-contained binary: no runtime, packages or services to
// install. Typing uses each OS's native input API (SendInput on Windows,
// CGEvent on macOS, XTest or uinput on Linux).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

var version = "dev" // set with -ldflags "-X main.version=…"

const usage = `Voice Keyboard desktop client %s

Usage:
  voice-keyboard enroll --server URL --token TOKEN   pair this computer
  voice-keyboard start       start now and at every login
  voice-keyboard stop        stop and do not start at login
  voice-keyboard status      show whether it is running and connected
  voice-keyboard logs        show recent log lines and follow new ones (-n 50, --no-follow)
  voice-keyboard type-test   type a test string after 3 seconds
  voice-keyboard uninstall   stop and remove settings and login item
  voice-keyboard run         run in the foreground (used by the login item)
  voice-keyboard version
`

func main() {
	if len(os.Args) < 2 {
		fmt.Printf(usage, version)
		os.Exit(2)
	}
	var err error
	switch cmd, args := os.Args[1], os.Args[2:]; cmd {
	case "enroll":
		err = enrollCommand(args)
	case "start":
		err = startCommand()
	case "stop":
		err = stopCommand()
	case "status":
		err = statusCommand()
	case "logs":
		err = logsCommand(args)
	case "run":
		err = runCommand()
	case "type-test":
		err = typeTestCommand(strings.Join(args, " "))
	case "uninstall":
		err = uninstallCommand()
	case "version", "--version", "-v":
		fmt.Println(version)
	case "help", "--help", "-h":
		fmt.Printf(usage, version)
	default:
		fmt.Fprintf(os.Stderr, "Unknown command %q\n\n"+usage, cmd, version)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "voice-keyboard:", err)
		os.Exit(1)
	}
}

func enrollCommand(args []string) error {
	flags := flag.NewFlagSet("enroll", flag.ContinueOnError)
	server := flags.String("server", "", "backend URL, e.g. https://voice.example.com")
	token := flags.String("token", "", "install token from Settings → Computers")
	name := flags.String("name", "", "name shown in the web app (default: host name)")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *server == "" || *token == "" {
		return errors.New("--server and --token are required")
	}
	if *name == "" {
		*name, _ = os.Hostname()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	cfg, err := enroll(ctx, strings.TrimRight(*server, "/"), *token, *name)
	if err != nil {
		return err
	}
	if err := saveConfig(cfg); err != nil {
		return err
	}
	fmt.Printf("Paired as %q with %s\n", cfg.Client, cfg.Server)
	return nil
}

func startCommand() error {
	if _, err := loadConfig(); err != nil {
		return err
	}
	if err := enableAutostart(); err != nil {
		return fmt.Errorf("could not register the login item: %w", err)
	}
	if err := waitForState(func(s state) bool { return s.running() }, 5*time.Second); err != nil {
		return errors.New("started, but it has not reported in yet; see " + logPath())
	}
	fmt.Println("Running. It starts automatically at login.")
	time.Sleep(time.Second) // the agent checks its typing permission right after starting
	if w := readState().Warning; w != "" {
		fmt.Println("Action needed:", w)
	}
	return nil
}

func stopCommand() error {
	if err := disableAutostart(); err != nil {
		return err
	}
	if s := readState(); s.running() {
		if err := stopProcess(s.PID); err != nil {
			return err
		}
	}
	_ = waitForState(func(s state) bool { return !s.running() }, 5*time.Second)
	fmt.Println("Stopped. It will not start at login until you run: voice-keyboard start")
	return nil
}

func statusCommand() error {
	cfg, err := loadConfig()
	if err != nil {
		fmt.Println("Not paired. Run the install command from the web app: Settings → Computers.")
		return nil
	}
	s := readState()
	fmt.Printf("Computer:   %s\nServer:     %s\n", cfg.Client, cfg.Server)
	switch {
	case !s.running():
		fmt.Println("Running:    no (start it with: voice-keyboard start)")
	case s.Connected:
		fmt.Printf("Running:    yes, connected since %s\n", s.Since.Local().Format("Jan 2 15:04"))
		if s.Selected != nil {
			fmt.Printf("Typing:     %s\n", map[bool]string{true: "yes, this computer types dictation",
				false: "no, another computer is selected (Settings → Computers in the web app)"}[*s.Selected])
		}
	default:
		fmt.Printf("Running:    yes, not connected (%s)\n", s.LastError)
	}
	fmt.Printf("Login item: %s\n", map[bool]string{true: "on", false: "off"}[autostartEnabled()])
	fmt.Printf("Input:      %s\n", typingBackendName())
	if s.running() && s.Warning != "" {
		fmt.Println("Action:    ", s.Warning)
	}
	fmt.Printf("Log:        %s\n", logPath())
	return nil
}

func runCommand() error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	if s := readState(); s.running() && s.PID != os.Getpid() {
		return fmt.Errorf("already running (pid %d)", s.PID)
	}
	closeLog := redirectLogIfDetached()
	defer closeLog()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	defer clearState()
	return serve(ctx, cfg)
}

func typeTestCommand(text string) error {
	if text == "" {
		text = "Voice Keyboard test"
	}
	if err := checkTypingPermission(true); err != nil {
		return err
	}
	kb, err := newKeyboard()
	if err != nil {
		return err
	}
	defer kb.Close()
	fmt.Println("Typing in 3 seconds; click into a text field now.")
	time.Sleep(3 * time.Second)
	return kb.Type(text)
}

func uninstallCommand() error {
	_ = stopCommand()
	if err := os.RemoveAll(configDir()); err != nil {
		return err
	}
	exe, _ := os.Executable()
	fmt.Printf("Removed settings and the login item. Delete the program itself with:\n  %s\n", removeHint(exe))
	return nil
}

func waitForState(ok func(state) bool, timeout time.Duration) error {
	for end := time.Now().Add(timeout); time.Now().Before(end); time.Sleep(200 * time.Millisecond) {
		if ok(readState()) {
			return nil
		}
	}
	return errors.New("timed out")
}
