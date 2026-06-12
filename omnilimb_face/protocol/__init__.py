"""omnilimb_face.protocol — the Open-LLM-VTuber ``/client-ws`` protocol.

Modules in this subpackage (added by later tasks) define the protocol event
data models, the (de)serialization gateway with round-trip guarantees and
error classification, and the message router that dispatches parsed client
events to the appropriate subsystem (Requirement 9).
"""
