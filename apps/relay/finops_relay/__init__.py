"""The Bale relay: one official Bale bot for every FinOps agent installation that opts in.

An installation registers once and keeps an outgoing long-poll open, so it needs no open port. A Bale chat links to
an installation (and one of its organizations) with a one-time code. The relay passes commands down and answers up;
it keeps who is linked to what, and the messages waiting in transit, never anyone's cost data.
"""
