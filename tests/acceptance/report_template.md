# Emma acceptance run

- **Mode:** {{MODE}}
- **When:** {{TIMESTAMP}}

## Summary

| Status | Count |
|--------|-------|
{{COUNTS}}

| ID  | Name | Status | Latency |
|-----|------|--------|---------|
{{SUMMARY_ROWS}}

## Scenarios

{{DETAILS}}

---

_v1 is shippable when every scenario in live mode is PASS (SKIPs allowed only for environment-blocked scenarios — see `live_blocked_by` in `scenarios.yaml`)._
