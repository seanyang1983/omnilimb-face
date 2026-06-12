"""omnilimb_face.voice — microphone capture, VAD segmentation and wake-word.

Modules in this subpackage (added by later tasks) implement the hands-free
voice pipeline: audio source enumeration/capture, voice-activity-detection
segmentation, and optional wake-word gating. All of these depend on the
optional ``[voice]`` / ``[wakeword]`` extras and degrade gracefully when the
underlying packages are absent (Requirement 12).
"""
