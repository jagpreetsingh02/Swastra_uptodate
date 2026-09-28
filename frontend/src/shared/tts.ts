/**
 * Speaking a question aloud, on a browser engine that fights you at every step.
 *
 * `speechSynthesis.speak()` looks like a one-liner and is not. The "Read aloud button is
 * visible but nothing is heard" bug was not one fault, it was five, and each of them fails
 * *silently* — no exception, no console warning, just no sound. They are documented here
 * because every one of them will look like a mystery again in six months.
 *
 *  1. `getVoices()` IS EMPTY ON FIRST CALL. Voices load asynchronously. The old code called
 *     `getVoices().find(...)` during the first render, got an empty array, and set no voice.
 *     For `en-IN` the engine picks a default and you get away with it; for `hi-IN` and
 *     `ta-IN` there is often no default and the utterance is dropped without a word.
 *     Fixed by waiting for `voiceschanged`, with a poll behind it because Safari fires that
 *     event inconsistently and sometimes not at all.
 *
 *  2. AUTOPLAY IS BLOCKED UNTIL THE USER TOUCHES SOMETHING. The first question spoke from a
 *     `useEffect` on mount — no gesture had happened, so the utterance was discarded. Fixed
 *     by `unlock()`, called from the first real tap anywhere in the kiosk, which primes the
 *     engine with a silent utterance so later automatic prompts are allowed.
 *
 *  3. CANCEL IMMEDIATELY FOLLOWED BY SPEAK LOSES THE UTTERANCE. Chromium's queue needs a
 *     tick between the two. `speechSynthesis.cancel(); speechSynthesis.speak(u)` on the same
 *     line drops `u` perhaps half the time — which is exactly the "sometimes it works"
 *     symptom that makes this so annoying to chase.
 *
 *  4. THE UTTERANCE IS GARBAGE COLLECTED MID-SENTENCE. Chromium holds only a weak reference.
 *     A local `const utterance` can be collected while speaking, cutting the audio off and
 *     never firing `onend`. It has to be kept alive on a module-level reference.
 *
 *  5. CHROMIUM STOPS AFTER ABOUT FIFTEEN SECONDS. A long prompt simply halts mid-word. The
 *     documented workaround is a `pause()`/`resume()` pump while speaking.
 *
 * On top of all that, React 18 StrictMode double-invokes effects in development: mount,
 * cleanup, mount. A `useEffect` that spoke on mount and cancelled on cleanup therefore
 * cancelled its own speech, and the second invocation saw "already spoken for this turn" and
 * stayed silent. That one is fixed in `QuestionCard`, not here.
 *
 * Nothing in this file captures audio or needs consent — reading a question aloud records
 * nothing. Only the microphone is consent-gated, in `useSpeech`.
 */

/** BCP-47 tags. The Web Speech API wants a region, not a bare ISO 639-1 code. */
export const LOCALES: Record<string, string> = {
  en: 'en-IN',
  hi: 'hi-IN',
  bn: 'bn-IN',
  ta: 'ta-IN',
  te: 'te-IN',
  mr: 'mr-IN',
  kn: 'kn-IN',
  ml: 'ml-IN',
  gu: 'gu-IN',
  pa: 'pa-IN',
};

export function localeFor(language: string): string {
  return LOCALES[language] ?? 'en-IN';
}

export type SpeakStatus =
  | 'spoken'
  | 'unsupported'
  | 'no-voice'
  | 'blocked'
  | 'cancelled'
  | 'failed';

export interface SpeakResult {
  status: SpeakStatus;
  /** The voice actually used, for the diagnostics panel. */
  voice: string | null;
  /** True when the language asked for was not available and English was used instead. */
  fellBackToEnglish: boolean;
}

function synth(): SpeechSynthesis | null {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
    ? window.speechSynthesis
    : null;
}

export function ttsSupported(): boolean {
  return synth() !== null;
}

// ---------------------------------------------------------------- voices

let voicesPromise: Promise<SpeechSynthesisVoice[]> | null = null;

/**
 * The voice list, once it actually exists.
 *
 * Belt and braces on purpose: `voiceschanged` is the documented signal and Chrome fires it,
 * Safari sometimes populates the list without firing anything, and a headless/CI browser may
 * never have voices at all. So: resolve immediately if the list is already there, otherwise
 * race the event against a poll, and give up after a second rather than hanging the prompt.
 */
export function loadVoices(): Promise<SpeechSynthesisVoice[]> {
  if (voicesPromise) return voicesPromise;
  const engine = synth();
  if (!engine) return Promise.resolve([]);

  voicesPromise = new Promise((resolve) => {
    const existing = engine.getVoices();
    if (existing.length) {
      resolve(existing);
      return;
    }

    let settled = false;
    const finish = (voices: SpeechSynthesisVoice[]) => {
      if (settled) return;
      settled = true;
      clearInterval(poll);
      clearTimeout(giveUp);
      engine.removeEventListener('voiceschanged', onChange);
      resolve(voices);
    };
    const onChange = () => finish(engine.getVoices());
    engine.addEventListener('voiceschanged', onChange);

    const poll = setInterval(() => {
      const voices = engine.getVoices();
      if (voices.length) finish(voices);
    }, 100);

    // A browser with no voices installed must not leave the question silent forever with a
    // spinner. Resolve empty and let the caller report "no voice" honestly.
    const giveUp = setTimeout(() => finish(engine.getVoices()), 1000);
  });
  return voicesPromise;
}

/**
 * Best voice for a language, and whether we had to fall back to English.
 *
 * Matching is widened deliberately: `hi-IN` exactly, then any `hi-*`, then any voice whose
 * name mentions the language. Engines are wildly inconsistent about how they tag Indic
 * voices, and a patient who gets Hindi read in a slightly wrong regional voice is far better
 * served than one who gets silence.
 */
export function pickVoice(
  voices: SpeechSynthesisVoice[],
  language: string,
): { voice: SpeechSynthesisVoice | null; fellBackToEnglish: boolean } {
  const locale = localeFor(language).toLowerCase();
  const base = language.toLowerCase();

  const exact = voices.find((v) => v.lang.toLowerCase() === locale);
  if (exact) return { voice: exact, fellBackToEnglish: false };

  const sameLanguage = voices.find((v) => v.lang.toLowerCase().startsWith(`${base}-`));
  if (sameLanguage) return { voice: sameLanguage, fellBackToEnglish: false };

  const bare = voices.find((v) => v.lang.toLowerCase() === base);
  if (bare) return { voice: bare, fellBackToEnglish: false };

  if (base === 'en') {
    const anyEnglish = voices.find((v) => v.lang.toLowerCase().startsWith('en'));
    return { voice: anyEnglish ?? null, fellBackToEnglish: false };
  }

  // No voice for this language. English is a poor substitute for a Tamil speaker, so the
  // caller is told, and the UI says so rather than pretending the prompt was read.
  const english =
    voices.find((v) => v.lang.toLowerCase() === 'en-in') ??
    voices.find((v) => v.lang.toLowerCase().startsWith('en')) ??
    null;
  return { voice: english, fellBackToEnglish: english !== null };
}

/**
 * Whether this device can read `language` in a voice OF that language. An English fallback
 * does not count — that is the case `hostedVoice` exists to replace.
 */
export async function hasNativeVoice(language: string): Promise<boolean> {
  if (!synth()) return false;
  const voices = await loadVoices();
  const { voice, fellBackToEnglish } = pickVoice(voices, language);
  return voice !== null && !fellBackToEnglish;
}

// ---------------------------------------------------------------- autoplay

let unlocked = false;

/**
 * Prime the engine from a real user gesture, so later automatic prompts are permitted.
 *
 * Browsers refuse audio that no user action asked for. The kiosk's first prompt is exactly
 * that — it speaks as soon as the question renders — so without this the first question of
 * every session is silent, and often every question after it too, because the engine stays
 * in a blocked state. Speaking a single space from inside a click handler satisfies the
 * policy and is inaudible.
 *
 * Safe to call repeatedly; only the first call does anything.
 */
export function unlock(): void {
  const engine = synth();
  if (!engine || unlocked) return;
  unlocked = true;
  try {
    const primer = new SpeechSynthesisUtterance(' ');
    primer.volume = 0;
    engine.speak(primer);
    engine.cancel();
  } catch {
    /* an engine that refuses the primer will refuse the prompt too; speak() reports it */
  }
  void loadVoices();
}

export function isUnlocked(): boolean {
  return unlocked;
}

// ---------------------------------------------------------------- speaking

/** See fault 4: a collected utterance stops mid-word and never fires `onend`. */
let current: SpeechSynthesisUtterance | null = null;
let pump: ReturnType<typeof setInterval> | null = null;

function stopPump(): void {
  if (pump !== null) {
    clearInterval(pump);
    pump = null;
  }
}

/** Whether an utterance is still being held (and therefore not collected). See fault 4. */
export function hasPendingUtterance(): boolean {
  return current !== null;
}

export function cancelSpeech(): void {
  const engine = synth();
  stopPump();
  current = null;
  if (engine) engine.cancel();
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Speak text, and report honestly whether it was actually said.
 *
 * The return value matters: the UI needs to distinguish "read aloud" from "tried to read
 * aloud and produced nothing", because the second one must show the patient a way forward
 * rather than a button that appears to do nothing.
 */
export async function speak(text: string, language: string): Promise<SpeakResult> {
  const engine = synth();
  const trimmed = text.trim();
  if (!engine) return { status: 'unsupported', voice: null, fellBackToEnglish: false };
  if (!trimmed) return { status: 'cancelled', voice: null, fellBackToEnglish: false };

  engine.cancel();
  stopPump();
  // Fault 3: the queue needs a tick between cancel and speak, or this utterance is dropped.
  await sleep(60);

  const voices = await loadVoices();
  const { voice, fellBackToEnglish } = pickVoice(voices, language);

  if (!voices.length) {
    return { status: 'no-voice', voice: null, fellBackToEnglish: false };
  }

  const utterance = new SpeechSynthesisUtterance(trimmed);
  utterance.lang = voice?.lang ?? localeFor(language);
  if (voice) utterance.voice = voice;
  utterance.rate = 0.92; // a touch slower than default: this is read to an elderly patient
  utterance.pitch = 1;
  utterance.volume = 1; // explicit, because a stale 0 from the primer is invisible to debug

  current = utterance; // fault 4
  return new Promise<SpeakResult>((resolve) => {
    let spokeAtAll = false;
    let settled = false;
    const done = (status: SpeakStatus) => {
      if (settled) return;
      settled = true;
      stopPump();
      current = null;
      resolve({ status, voice: voice?.name ?? null, fellBackToEnglish });
    };

    utterance.onstart = () => {
      spokeAtAll = true;
    };
    utterance.onend = () => done(spokeAtAll ? 'spoken' : 'blocked');
    utterance.onerror = (event) => {
      // `interrupted` and `canceled` are us calling cancel(), not a failure to report.
      const reason = (event as SpeechSynthesisErrorEvent).error;
      done(reason === 'interrupted' || reason === 'canceled' ? 'cancelled' : 'failed');
    };

    try {
      engine.speak(utterance);
    } catch {
      done('failed');
      return;
    }

    // Fault 5: Chromium halts around fifteen seconds unless nudged.
    pump = setInterval(() => {
      if (!engine.speaking) return;
      engine.pause();
      engine.resume();
    }, 10000);

    // If nothing has started after a generous wait, the engine took the utterance and threw
    // it away — the silent-failure mode this whole module exists to catch.
    setTimeout(() => {
      if (!spokeAtAll && !settled) done('blocked');
    }, 3000);
  });
}
