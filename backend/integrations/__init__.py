"""backend/integrations : the source-connector layer.

Build-your-own OAuth + connected-account framework for pulling relationship CONTEXT
from external sources (Google = Gmail + Calendar first; Zoom etc. later). Each
source authorizes per-host, stores refreshable tokens, and writes facts/timeline
into the spine -- the deterministic foundation under the roadmap's integration items.
We poll (via the existing scheduler), so there's no webhook/Pub-Sub plumbing.
"""
