"""
Agents — one module per mechanism stage.

    prospector  stage 02   concurrent fan-out across source adapters
    scorer      stage 03a  fit score + floating threshold
    outreach    stage 03b  autonomous compose / send / track
    matcher     stage 04   symbiotic + affinity edges, group formation
    roi         stage 05   verified conversion ledger + net ROI

`sources/` holds the pluggable prospect-source adapters the prospector uses.
"""
