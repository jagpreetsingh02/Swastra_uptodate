/**
 * Reading a prompt aloud with the server's voice, for the languages this device cannot speak.
 *
 * The browser voice stays first (ADR-0010: offline is the default). But a Hindi or Tamil
 * patient on a device with no voice for their language used to get silence, or — worse — an
 * English voice mangling Devanagari. The server's `/speak` is backed by Bhashini (ADR-0018),
 * which has a voice for every language the kiosk offers.
 *
 * Every failure here resolves to a status rather than throwing, so the caller can fall back to
 * the browser path and the patient is never worse off than without this module.
 */
import { api } from './api';
import type { SpeakResult, SpeakStatus } from './tts';

/**
 * Bumped by every cancel and every new prompt. A prompt whose fetch returns after its
 * generation has moved on was stopped (Stop, barge-in, or the next question) and must not
 * start playing — the network round trip is exactly the window a patient talks into.
 */
let generation = 0;
/** Held at module level so Stop and barge-in can reach it. One prompt plays at a time. */
let playing: HTMLAudioElement | null = null;
let playingUrl: string | null = null;
let settle: ((status: SpeakStatus) => void) | null = null;

function release(): void {
  if (playing) {
    playing.onended = null;
    playing.onerror = null;
    playing.pause();
  }
  if (playingUrl) URL.revokeObjectURL(playingUrl);
  playing = null;
  playingUrl = null;
  settle = null;
}

export function cancelHosted(): void {
  generation += 1;
  const pending = settle;
  release();
  // Resolve, never abandon: the caller is awaiting this prompt, like the browser path whose
  // cancel fires `onerror('canceled')`.
  pending?.('cancelled');
}

function toBlob(base64: string, mediaType: string): Blob {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  return new Blob([bytes], { type: mediaType || 'audio/wav' });
}

const result = (status: SpeakStatus, voice: string | null = null): SpeakResult => ({
  status,
  voice,
  fellBackToEnglish: false,
});

export async function speakHosted(sessionRef: string, text: string): Promise<SpeakResult> {
  const trimmed = text.trim();
  if (!trimmed) return result('cancelled');

  cancelHosted();
  const mine = generation;
  let response;
  try {
    response = await api.speak(sessionRef, trimmed);
  } catch {
    return result(mine === generation ? 'failed' : 'cancelled');
  }
  if (mine !== generation) return result('cancelled');
  if (!response.audioBase64 || response.clientFallback) return result('no-voice');

  const voice = `server:${response.backend}`;
  const url = URL.createObjectURL(toBlob(response.audioBase64, response.mediaType));
  const audio = new Audio(url);
  playing = audio;
  playingUrl = url;

  return new Promise<SpeakResult>((resolve) => {
    settle = (status) => resolve(result(status, voice));
    const finish = (status: SpeakStatus) => {
      if (playing !== audio) return; // already settled by a cancel
      const done = settle;
      release();
      done?.(status);
    };
    audio.onended = () => finish('spoken');
    audio.onerror = () => finish('failed');
    audio.play().catch((error: unknown) => {
      finish(
        error instanceof DOMException && error.name === 'NotAllowedError' ? 'blocked' : 'failed',
      );
    });
  });
}
