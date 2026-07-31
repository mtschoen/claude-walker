package main

import "testing"

func TestEffectiveWorkerCount(t *testing.T) {
	tests := []struct {
		name           string
		processorCount int
		want           int
	}{
		{name: "below limit", processorCount: 4, want: 4},
		{name: "at limit", processorCount: 8, want: 8},
		{name: "above limit", processorCount: 16, want: 8},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := effectiveWorkerCount(test.processorCount); got != test.want {
				t.Fatalf("effectiveWorkerCount(%d) = %d; want %d",
					test.processorCount, got, test.want)
			}
		})
	}
}

func TestEffectiveEventWorkerCount(t *testing.T) {
	tests := []struct {
		name           string
		processorCount int
		groupCount     int
		want           int
	}{
		{name: "shrinks to nonzero group count", processorCount: 16, groupCount: 3, want: 3},
		{name: "keeps processor bound", processorCount: 4, groupCount: 10, want: 4},
		{name: "keeps workers for empty groups", processorCount: 4, groupCount: 0, want: 4},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := effectiveEventWorkerCount(test.processorCount, test.groupCount); got != test.want {
				t.Fatalf("effectiveEventWorkerCount(%d, %d) = %d; want %d",
					test.processorCount, test.groupCount, got, test.want)
			}
		})
	}
}
