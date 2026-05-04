#!/usr/bin/env python3
"""recorder-py — bubbaloop node that records Zenoh CBOR/JSON traffic to MCAP files."""

if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    from recorder.node import RecorderNode

    run_node(RecorderNode)
