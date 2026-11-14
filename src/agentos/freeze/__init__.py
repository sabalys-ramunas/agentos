"""Break-glass freeze: one action stops every agent in a lane, a workspace, a node or the mesh.

See service.py for what a freeze does, store.py for where the flag lives (it must still be
settable when the disk is full or the policy database is broken), and tools.py for lane_freeze.
"""
