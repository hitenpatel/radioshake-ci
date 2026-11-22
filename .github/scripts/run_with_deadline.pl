#!/usr/bin/perl
# Runs a command in its own process group and kills the whole group when it outlives the deadline (A11y-2 Task 0.4).
# Usage: run_with_deadline.pl <seconds> <command> [args...]
# Exit: the command's own status, or 142 when the deadline killed it (TERM to the group, KILL 15 s later).
# macOS runners have no GNU timeout; a plain `perl -e 'alarm ...; exec'` killed only xcodebuild and left its helpers
# holding the simulator, so the next audit class timed out on its first UI query (canary run 45051959419).
use strict;
use warnings;
use POSIX ":sys_wait_h";

my $secs = shift @ARGV;
die "usage: run_with_deadline.pl <seconds> <command> [args...]\n" unless defined $secs && $secs =~ /^\d+$/ && @ARGV;

my $pid = fork();
die "fork failed: $!\n" unless defined $pid;
if ($pid == 0) {
    setpgrp(0, 0);
    exec { $ARGV[0] } @ARGV or die "exec $ARGV[0] failed: $!\n";
}

my $deadline = time + $secs;
while (1) {
    my $done = waitpid($pid, WNOHANG);
    if ($done == $pid) {
        exit($? & 127 ? 128 + ($? & 127) : $? >> 8);
    }
    last if time >= $deadline;
    select(undef, undef, undef, 0.5);
}

kill 'TERM', -$pid;
my $grace = time + 15;
while (time < $grace) {
    last if waitpid($pid, WNOHANG) == $pid;
    select(undef, undef, undef, 0.5);
}
kill 'KILL', -$pid;
waitpid($pid, 0);
exit 142;
