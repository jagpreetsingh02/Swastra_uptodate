/**
 * One question, all three ways to answer it, on one screen.
 *
 * The whole of Module A's patient-facing contract lives in this component: the prompt is
 * spoken aloud, the tap options are always visible, the microphone is offered alongside them
 * rather than instead of them, and a low-confidence transcript re-presents the question with
 * an explanation instead of recording a guess.
 *
 * ONE TAP ANSWERS. A single-choice option, a Yes/No, a face on the pain scale — each of those
 * is a complete answer, and it now submits on the tap that gives it. The Continue button that
 * used to follow was not a harmless extra click: the patient tapped an option, the screen did
 * not move, and nothing told them whether the machine had heard them. Multi-select keeps a
 * Done button because "I have finished choosing" is genuinely information the interface
 * cannot infer.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Question, VoiceOutcome } from '../shared/api';
import { Icon } from '../shared/Icon';
import { unlock } from '../shared/tts';
import { useSpeech } from '../shared/useSpeech';
import { FaceScale } from './FaceScale';
import { TapGrid } from './TapGrid';
import { TypedAnswer } from './TypedAnswer';
import { VoiceButton } from './VoiceButton';
import { Button } from '../design/ui';
import { motion } from 'motion/react';
import { press } from '../design/motion';

interface Props {
  question: Question;
  /** Lets a prompt be read by the server voice when this device has none for the language. */
  sessionRef: string;
  voice: VoiceOutcome | null;
  busy: boolean;
  /** Whether the patient granted the `voice` consent scope. When they did not, the
   *  microphone is not offered at all — showing a button that 403s is worse than not
   *  showing it, and the patient explicitly said no. */
  voiceEnabled: boolean;
  /** True when this question is being corrected rather than asked for the first time. */
  reopened?: boolean;
  /** The answer already on file, so a correction starts from what the patient said. */
  currentAnswer?: { value: unknown; verbatim: string | null; declined: boolean } | null;
  /** False on the very first question of the interview, where Back means nothing. */
  canGoBack?: boolean;
  onAnswer: (value: unknown) => void;
  onTyped: (value: string) => void;
  onSpoken: (transcript: string, confidence: number | null, bargeIn: boolean) => void;
  onSkip: () => void;
  onBack: () => void;
}

export function QuestionCard({
  question,
  sessionRef,
  voice,
  busy,
  voiceEnabled,
  reopened,
  currentAnswer,
  canGoBack,
  onAnswer,
  onTyped,
  onSpoken,
  onSkip,
  onBack,
}: Props): JSX.Element {
  const [selected, setSelected] = useState<string[]>([]);
  const [typed, setTyped] = useState('');
  const speech = useSpeech(question.language, sessionRef);
  const spokenFor = useRef<string | null>(null);

  const multi = question.kind === 'multi_choice';
  const degraded = question.touchOnly || Boolean(voice?.degradedToTouch);

  // Speech SYNTHESIS is always allowed: reading a question aloud captures nothing, so it
  // needs no consent. Only recognition — the microphone — is gated.
  //
  // Keyed on turnId, not questionId, so a re-presented question is read again — the patient
  // needs to hear it a second time, not be left in silence wondering what happened.
  //
  // NOTE the missing cleanup. This effect used to `return () => speech.cancelSpeech()`, and
  // under React 18 StrictMode that was the entire "no sound is heard" bug: the effect runs,
  // cleans up, and runs again in development, so the first invocation spoke, the cleanup
  // cancelled it, and the second invocation saw `spokenFor.current === turnId` and returned
  // without speaking. Every question, silent. Cancelling on unmount belongs with the mic
  // teardown in `useSpeech`, which already does it.
  useEffect(() => {
    setTyped('');
    // A reopened question starts from the answer already on file, so the patient sees what
    // they are changing instead of a blank screen that looks like lost work.
    const existing = reopened && currentAnswer?.value != null ? currentAnswer.value : null;
    setSelected(
      existing == null
        ? []
        : Array.isArray(existing)
          ? existing.map(String)
          : [String(existing)],
    );

    if (spokenFor.current === question.turnId) return;
    spokenFor.current = question.turnId;
    const help = question.help ? ` ${question.help}` : '';
    void speech.speak(`${question.prompt}${help}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question.turnId]);

  const listen = useCallback(() => {
    unlock();
    speech.cancelSpeech();
    speech.start(({ transcript, confidence, bargeIn }) => {
      onSpoken(transcript, confidence, bargeIn);
    });
  }, [speech, onSpoken]);

  const hearQuestion = useCallback(() => {
    // From a real tap, so this also satisfies the autoplay policy for every later prompt.
    unlock();
    const help = question.help ? ` ${question.help}` : '';
    void speech.speak(`${question.prompt}${help}`);
  }, [speech, question.prompt, question.help]);

  return (
    <>
      <div className="kx-question-bar">
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          disabled={busy || canGoBack === false}
          aria-label="Go back to the previous question"
          icon={<Icon name="arrowLeft" />}
        >
          Back
        </Button>

        {speech.canSpeak && (
          <Button
            variant={speech.speaking ? 'secondary' : 'quiet'}
            size="sm"
            icon={<Icon name="speaker" />}
            onClick={speech.speaking ? speech.cancelSpeech : hearQuestion}
          >
            {speech.speaking ? 'Stop' : 'Hear the question'}
          </Button>
        )}
      </div>

      <p className="kx-eyebrow">
        {question.sectionTitle}
        {question.socrates && ` · ${question.socrates}`}
      </p>

      <h1 className="kx-question" lang={question.language}>
        {question.prompt}
      </h1>
      {question.help && <p className="kx-question__hint">{question.help}</p>}

      {reopened && (
        <div className="reopened-note" role="status">
          <Icon name="check" />
          <div>
            You are changing this answer.
            {currentAnswer?.verbatim && (
              <>
                {' '}
                You said: <strong>“{currentAnswer.verbatim}”</strong>
              </>
            )}
          </div>
        </div>
      )}

      {question.translationMissing && (
        <p className="kiosk-help" style={{ color: 'var(--warn)' }}>
          This question is not yet translated into your language and is shown in English.
        </p>
      )}

      {/* Silence is a failure the patient can see coming only if we say so. */}
      {speech.speechNotice && (
        <p className="kiosk-help" style={{ color: 'var(--warn)' }}>
          {speech.speechNotice}
        </p>
      )}

      {voice?.degradedToTouch && voice.prompt && (
        <div className="voice-degraded" role="status">
          <Icon name="mic" />
          <div>
            <strong>{voice.prompt}</strong>
            {voice.transcript.text && (
              <div style={{ fontSize: 19, marginTop: 8, color: 'var(--ink-2)' }}>
                I heard: “{voice.transcript.text}” —{' '}
                {voice.transcript.confidence === null
                  ? 'but this device did not tell me how sure it was, and I will not guess.'
                  : `but only ${Math.round(
                      voice.transcript.confidence * 100,
                    )}% sure, and I will not guess.`}
              </div>
            )}
          </div>
        </div>
      )}

      <div style={{ marginTop: 24 }}>
        {question.kind === 'boolean' ? (
          <div className="kx-options kx-options--pair" role="radiogroup">
            {[
              { value: 'true', label: 'Yes', icon: 'check', answer: true },
              { value: 'false', label: 'No', icon: 'cross', answer: false },
            ].map((choice) => (
              <motion.button
                key={choice.value}
                type="button"
                className="kx-option"
                role="radio"
                aria-checked={selected[0] === choice.value}
                disabled={busy}
                whileTap={busy ? undefined : press}
                onClick={() => {
                  setSelected([choice.value]);
                  onAnswer(choice.answer);
                }}
              >
                <span className="kx-option__glyph" aria-hidden="true">
                  <Icon name={choice.icon} />
                </span>
                <span>{choice.label}</span>
              </motion.button>
            ))}
          </div>
        ) : question.kind === 'scale' && question.scale ? (
          <FaceScale
            scale={question.scale}
            language={question.language}
            value={selected.length ? Number(selected[0]) : null}
            onSelect={(value) => {
              setSelected([String(value)]);
              onAnswer(value);
            }}
          />
        ) : question.options.length ? (
          <TapGrid
            options={question.options}
            selected={selected}
            multi={multi}
            busy={busy}
            onSelect={setSelected}
            onAnswer={(value) => onAnswer(value)}
          />
        ) : (
          <TypedAnswer
            value={typed}
            placeholder="Type here, or use the microphone below"
            busy={busy}
            onChange={setTyped}
            onSubmit={onTyped}
          />
        )}
      </div>

      {question.kind === 'open_text' && question.options.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <p className="kiosk-help" style={{ marginBottom: 12 }}>
            Or describe it in your own words:
          </p>
          <TypedAnswer
            value={typed}
            placeholder="Type here…"
            busy={busy}
            onChange={setTyped}
            onSubmit={onTyped}
          />
        </div>
      )}

      {voiceEnabled && !speech.unavailable && (
        <VoiceButton
          supported={speech.supported}
          listening={speech.listening}
          interim={speech.interim}
          disabled={busy || degraded}
          label="Speak my answer"
          onStart={listen}
          onStop={speech.stop}
        />
      )}
      {voiceEnabled && speech.error && (
        <div className="kiosk-error" style={{ marginTop: 16 }}>
          {speech.error}
        </div>
      )}

      <div className="kx-actions">
        {/* The ONLY closing action left, and only where the interface genuinely cannot know
            the patient has finished choosing. */}
        {multi && selected.length > 0 && (
          <Button size="lg" disabled={busy} onClick={() => onAnswer(selected)}>
            Done — {selected.length} selected
          </Button>
        )}
        {/* Deliberately small and quiet. Declining is always available and never
            the suggested path; at full button size it competed with the answers. */}
        <Button variant="ghost" size="sm" disabled={busy} onClick={onSkip}>
          I would rather not answer
        </Button>
      </div>
    </>
  );
}
