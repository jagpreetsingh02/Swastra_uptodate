/**
 * Speech on the kiosk device: recognition and synthesis, both in the browser.
 *
 * Why on-device rather than server-side: it needs no API key, no vendor, and no network, so
 * it keeps working when the venue wifi dies twenty minutes before judging. The backend still
 * applies the confidence policy to whatever transcript arrives here (see
 * `ClientSpeechBackend`), so a client cannot get a bad transcript accepted just by producing
 * it locally.
 *
 * Barge-in: the moment recognition detects speech, any prompt still being spoken is cancelled.
 * A patient should never have to wait for a machine to finish talking.
 *
 * The one exception to on-device: when the device has no voice OF the patient's language and
 * a session exists, the prompt is read by the server (Bhashini, ADR-0018) instead of left
 * silent or read by an English voice. If that fails, the browser path runs exactly as before.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { cancelHosted, speakHosted } from './hostedVoice';
import {
  cancelSpeech as cancelTts,
  hasNativeVoice,
  localeFor,
  speak as speakTts,
  ttsSupported,
  type SpeakResult,
} from './tts';

interface SpeechRecognitionAlternative { transcript: string; confidence: number }
interface SpeechRecognitionResult { 0: SpeechRecognitionAlternative; isFinal: boolean; length: number }
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: { length: number; [index: number]: SpeechRecognitionResult };
}
interface SpeechRecognitionLike extends EventTarget {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  onspeechstart: (() => void) | null;
}

type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export interface SpeechResult {
  transcript: string;
  /**
   * The browser's own score, or `null` when it did not give one.
   *
   * `null` is reported as `null`. An earlier version substituted 0.7 here because Chrome
   * reports 0 on several Indic locales and passing that through degraded every spoken
   * answer to touch. That was fabricating provenance: a confidence nobody measured, attached
   * to a clinical fact, indistinguishable downstream from one that was. The backend now
   * handles an unmeasured score explicitly — recording the fact as needing verification, and
   * degrading to touch outright on the questions where being wrong is dangerous.
   */
  confidence: number | null;
  bargeIn: boolean;
}

export interface UseSpeech {
  /** The constructor exists. NOT the same as "recognition works" — see `unavailable`. */
  supported: boolean;
  /** Proven not to work in this browser. Once true the caller must stop offering the mic. */
  unavailable: boolean;
  listening: boolean;
  interim: string;
  error: string | null;
  start(onResult: (result: SpeechResult) => void): void;
  stop(): void;
  speak(text: string): Promise<SpeakResult>;
  cancelSpeech(): void;
  /** True while a prompt is being read, so the UI can show it and offer Stop. */
  speaking: boolean;
  /** Speech synthesis exists in this browser. Independent of the microphone. */
  canSpeak: boolean;
  /**
   * Why the last prompt was not heard, in words a patient can act on. `null` when the
   * prompt was read, or when none has been attempted yet.
   */
  speechNotice: string | null;
}

/**
 * How long a `start()` may produce absolutely nothing before we call the engine dead.
 *
 * This exists because of a specific, nasty behaviour: in Chromium — and therefore in Brave,
 * Electron, and most kiosk browser builds — `webkitSpeechRecognition` *exists* and
 * constructs, but `start()` silently does nothing. No result, no error, not even `onend`.
 * The recogniser a feature check says is present never calls you back.
 *
 * Without a watchdog the patient taps the microphone, the button pulses "Listening…", and it
 * stays that way forever. On the kiosk's primary input mode that is the worst possible
 * failure: it looks like the machine is working, and it is not.
 *
 * A genuinely silent patient still triggers `onend` or a `no-speech` error well inside this
 * window, so this only fires on an engine that was never going to answer.
 */
const DEAD_ENGINE_MS = 6000;

/**
 * @param hostedRef The session to read prompts through when the device has no voice for the
 *   language. `null` before a session exists (the consent screen), which keeps the browser
 *   voice as the only option there.
 */
export function useSpeech(language: string, hostedRef: string | null = null): UseSpeech {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [speechNotice, setSpeechNotice] = useState<string | null>(null);

  const recognition = useRef<SpeechRecognitionLike | null>(null);
  const watchdog = useRef<ReturnType<typeof setTimeout> | null>(null);
  const speakingRef = useRef(false);
  const bargedIn = useRef(false);
  const supported = recognitionCtor() !== null;

  const clearWatchdog = useCallback(() => {
    if (watchdog.current !== null) {
      clearTimeout(watchdog.current);
      watchdog.current = null;
    }
  }, []);

  const cancelSpeech = useCallback(() => {
    cancelTts();
    cancelHosted();
    speakingRef.current = false;
    setSpeaking(false);
  }, []);

  const stop = useCallback(() => {
    clearWatchdog();
    recognition.current?.stop();
    setListening(false);
  }, [clearWatchdog]);

  const start = useCallback(
    (onResult: (result: SpeechResult) => void) => {
      const Ctor = recognitionCtor();
      if (!Ctor) {
        setError('This browser cannot listen. Please tap your answer instead.');
        return;
      }
      setError(null);
      setInterim('');
      bargedIn.current = false;

      const instance = new Ctor();
      instance.lang = localeFor(language);
      instance.continuous = false;
      instance.interimResults = true;
      instance.maxAlternatives = 1;

      instance.onspeechstart = () => {
        // The engine is alive: it heard something. Stand the watchdog down.
        clearWatchdog();
        // Barge-in: the patient started talking, so stop talking at them.
        if (speakingRef.current) {
          cancelSpeech();
          bargedIn.current = true;
        }
      };

      instance.onresult = (event) => {
        let finalText = '';
        let finalConfidence = 0;
        let interimText = '';
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const result = event.results[i];
          if (result.isFinal) {
            finalText += result[0].transcript;
            finalConfidence = result[0].confidence;
          } else {
            interimText += result[0].transcript;
          }
        }
        clearWatchdog();
        if (interimText) setInterim(interimText);
        if (finalText) {
          setInterim('');
          setListening(false);
          onResult({
            transcript: finalText.trim(),
            // Reported as-is, or null. Never substituted. See SpeechResult.confidence.
            confidence: finalConfidence > 0 ? finalConfidence : null,
            bargeIn: bargedIn.current,
          });
        }
      };

      instance.onerror = (event) => {
        clearWatchdog();
        setListening(false);
        setInterim('');
        if (event.error === 'service-not-allowed' || event.error === 'language-not-supported') {
          setUnavailable(true);
          setError('Speech is not available on this device. Please tap your answer.');
          return;
        }
        if (event.error === 'no-speech') {
          onResult({ transcript: '', confidence: 0, bargeIn: false });
          return;
        }
        if (event.error === 'not-allowed') {
          setError('The microphone is blocked. Please tap your answer instead.');
          return;
        }
        setError('Listening failed. Please tap your answer instead.');
      };

      instance.onend = () => {
        clearWatchdog();
        setListening(false);
      };

      recognition.current = instance;
      try {
        instance.start();
        setListening(true);
        // See DEAD_ENGINE_MS. If nothing at all comes back, the recogniser was never going
        // to answer — stop pretending to listen, and stop offering the button.
        watchdog.current = setTimeout(() => {
          try {
            instance.abort();
          } catch {
            /* already wedged; nothing to abort */
          }
          setListening(false);
          setInterim('');
          setUnavailable(true);
          setError('Speech is not available on this device. Please tap your answer.');
        }, DEAD_ENGINE_MS);
      } catch {
        setUnavailable(true);
        setError('Could not start listening. Please tap your answer instead.');
      }
    },
    [language, cancelSpeech, clearWatchdog],
  );

  const speak = useCallback(
    async (text: string): Promise<SpeakResult> => {
      setSpeaking(true);
      speakingRef.current = true;
      let result: SpeakResult | null = null;
      if (hostedRef && !(await hasNativeVoice(language))) {
        result = await speakHosted(hostedRef, text);
        // Anything but a clean outcome falls through to the browser, so the patient is never
        // worse off than without the server voice.
        if (result.status !== 'spoken' && result.status !== 'cancelled') result = null;
      }
      if (result === null) result = await speakTts(text, language);
      speakingRef.current = false;
      setSpeaking(false);

      // Silence is the failure mode this whole path exists to make visible. Say which kind
      // it was, in a sentence that tells the patient what to do instead.
      setSpeechNotice(
        result.status === 'spoken' || result.status === 'cancelled'
          ? null
          : result.status === 'no-voice'
            ? 'This device has no voice installed for reading aloud. Please read the question on screen.'
            : result.status === 'unsupported'
              ? 'This browser cannot read aloud. Please read the question on screen.'
              : result.status === 'blocked'
                ? 'Tap “Hear the question” to turn sound on.'
                : 'Could not read the question aloud. Please read it on screen.',
      );
      return result;
    },
    [language, hostedRef],
  );

  useEffect(
    () => () => {
      clearWatchdog();
      recognition.current?.abort();
      cancelSpeech();
    },
    [cancelSpeech, clearWatchdog],
  );

  return {
    supported,
    unavailable,
    listening,
    interim,
    error,
    start,
    stop,
    speak,
    cancelSpeech,
    speaking,
    canSpeak: ttsSupported() || hostedRef !== null,
    speechNotice,
  };
}
