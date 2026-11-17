# ConformU results

ASCOM Conform Universal **v4.5.3** (linux-arm64), run 2026-11-15 against this server
(Alpaca protocol check + full device conformance, Switch interface v3).

| Target | Alpaca protocol | Conformance |
|---|---|---|
| Mock board (4 channels, no hardware) | pass – 0 errors / 0 issues / 0 info | pass – no errors, warnings or issues |
| Real board, device 0 (`usb-xhci-hcd.1-1.3`) | pass | pass |
| Real board, device 1 (`usb-xhci-hcd.1-1.4.4`) | pass | pass |

Both real boards were all-off before the run and were all-off afterwards (ConformU switches every channel).

Files: `*-conformance.txt/.log/.json` (full report, log, machine-readable results) and
`*-alpaca-protocol.*` per target. Re-run with:

```bash
conformu alpacaprotocol http://localhost:11111/api/v1/switch/0
conformu conformance    http://localhost:11111/api/v1/switch/0
```

Note: a first mock protocol run once showed a single dropped connection (not reproducible in 5 further runs);
the listen backlog was raised from 5 to 128 as a precaution.
