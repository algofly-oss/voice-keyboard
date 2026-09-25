package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"time"
)

// logsCommand prints the last lines of the log and, unless --no-follow is
// given, keeps printing new lines as the running client writes them (like
// tail -f), surviving log rotation. Ctrl+C exits.
func logsCommand(args []string) error {
	flags := flag.NewFlagSet("logs", flag.ContinueOnError)
	lines := flags.Int("n", 50, "number of recent lines to show")
	noFollow := flags.Bool("no-follow", false, "print recent lines and exit")
	if err := flags.Parse(args); err != nil {
		return err
	}
	f, err := os.Open(logPath())
	if errors.Is(err, os.ErrNotExist) {
		if *noFollow {
			fmt.Println("No log yet:", logPath())
			return nil
		}
		fmt.Println("Waiting for the client to write", logPath())
	} else if err != nil {
		return err
	} else {
		if err := printTail(f, *lines); err != nil {
			return err
		}
	}
	if *noFollow {
		if f != nil {
			f.Close()
		}
		return nil
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	return follow(ctx, f)
}

// printTail prints the last n lines and leaves f positioned at its end.
func printTail(f *os.File, n int) error {
	info, err := f.Stat()
	if err != nil {
		return err
	}
	// Recent lines are near the end; 256 KB covers far more than 50 of them.
	start := max(info.Size()-256<<10, 0)
	if _, err := f.Seek(start, io.SeekStart); err != nil {
		return err
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return err
	}
	all := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if start > 0 && len(all) > 0 {
		all = all[1:] // first line is probably partial
	}
	for _, line := range all[max(len(all)-n, 0):] {
		if line != "" {
			fmt.Println(line)
		}
	}
	return nil
}

func follow(ctx context.Context, f *os.File) error {
	var reader *bufio.Reader
	if f != nil {
		reader = bufio.NewReader(f)
	}
	for {
		if reader != nil {
			for {
				line, err := reader.ReadString('\n')
				fmt.Print(line)
				if err != nil {
					break
				}
			}
		}
		select {
		case <-ctx.Done():
			if f != nil {
				f.Close()
			}
			return nil
		case <-time.After(500 * time.Millisecond):
		}
		// Reopen when the file appears or was rotated (it shrank or was replaced).
		info, err := os.Stat(logPath())
		if err != nil {
			continue
		}
		var pos int64
		if f != nil {
			pos, _ = f.Seek(0, io.SeekCurrent)
		}
		if f == nil || info.Size() < pos || !sameFile(f, info) {
			if f != nil {
				f.Close()
			}
			if f, err = os.Open(logPath()); err != nil {
				f = nil
				continue
			}
			reader = bufio.NewReader(f)
		}
	}
}

func sameFile(f *os.File, info os.FileInfo) bool {
	current, err := f.Stat()
	return err == nil && os.SameFile(current, info)
}
