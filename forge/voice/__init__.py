"""Voice stage (A33 foundation + A36 audio loop).

The A33 foundation (``forge.voice.base``) parses voice commands into
intents and routes them through the A33 permission system — voice is just
another request source and can never bypass permissions. A36 adds the
audio layer around it: deterministic audio primitives, a simulated
speech codec, speech-to-text, text-to-speech, wake-word detection, and
the :class:`VoiceSession` orchestration loop.

Every audio capability is labeled honestly: the simulated providers only
handle audio produced by the deterministic simulated codec and refuse
real speech; real recognizers/synthesizers plug in behind the same
protocols.
"""

from forge.voice.audio import (AudioChunk, AudioError, audio_level,
                               read_wav, validate_chunk)
from forge.voice.base import (VoiceCommand, VoiceCommandResult, VoiceIntent,
                              VoiceInterface, VoicePermission)
from forge.voice.codec import (CodecError, decode_text, encode_text,
                               speech_chunk, wake_chunk, wake_samples)
from forge.voice.session import VoiceLoopResult, VoiceSession
from forge.voice.synthesizer import (SimulatedSpeechSynthesizer,
                                     SynthesisError, TextToSpeechProvider,
                                     UnconfiguredSpeechSynthesizer)
from forge.voice.transcriber import (SimulatedSpeechToText,
                                     SpeechToTextProvider, Transcription,
                                     TranscriptionError,
                                     UnconfiguredSpeechToText)
from forge.voice.wake import (SimulatedWakeWordDetector,
                              UnconfiguredWakeWordDetector, WakeDetection,
                              WakeWordDetector, WakeWordError)

__all__ = [
    "AudioChunk",
    "AudioError",
    "CodecError",
    "SimulatedSpeechSynthesizer",
    "SimulatedSpeechToText",
    "SimulatedWakeWordDetector",
    "SpeechToTextProvider",
    "SynthesisError",
    "TextToSpeechProvider",
    "Transcription",
    "TranscriptionError",
    "UnconfiguredSpeechSynthesizer",
    "UnconfiguredSpeechToText",
    "UnconfiguredWakeWordDetector",
    "VoiceCommand",
    "VoiceCommandResult",
    "VoiceIntent",
    "VoiceInterface",
    "VoiceLoopResult",
    "VoicePermission",
    "VoiceSession",
    "WakeDetection",
    "WakeWordDetector",
    "WakeWordError",
    "audio_level",
    "decode_text",
    "encode_text",
    "read_wav",
    "speech_chunk",
    "validate_chunk",
    "wake_chunk",
    "wake_samples",
]
