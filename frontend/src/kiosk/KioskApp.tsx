/**
 * The kiosk flow:
 *
 *   language → ABHA → PATIENT MEMORY → consent → interview → review → done
 *
 * with document upload reachable from *inside* the interview at any point, not only at the
 * end. That placement is the fix for a real usability failure: the upload step used to sit
 * behind the `documents` consent toggle (off by default) AND behind thirty-odd questions, so
 * the most demonstrable feature in the product was effectively unreachable. It is now a
 * persistent action, and if the scope was declined it asks for that one permission in place.
 *
 * The component holds no clinical logic. Which question comes next is the backend's decision
 * (the deterministic state machine in Module A), and this file only renders what it is given
 * and posts back what the patient did. If the frontend ever starts deciding what to ask, the
 * invariant that the LLM cannot change question order has quietly become untrue, because the
 * UI would then be a second place where the interview is defined.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, setToken, type StepResponse, type VoiceOutcome } from '../shared/api';
import { Icon } from '../shared/Icon';
import { unlock } from '../shared/tts';
import { AbhaLogin } from './AbhaLogin';
import { ConsentGate } from './ConsentGate';
import { DocumentUpload } from './DocumentUpload';
import { PatientHome } from './PatientHome';
import { DoneScreen } from './DoneScreen';
import { PatientReview } from './PatientReview';
import { ProgressRail } from './ProgressRail';
import { QuestionCard } from './QuestionCard';
import { LanguagePicker } from './LanguagePicker';
import { KioskShell } from '../design/KioskShell';
import { Button, Chip } from '../design/ui';

type Stage =
  | 'language'
  | 'login'
  | 'memory'
  | 'consent'
  | 'interview'
  | 'documents'
  | 'review'
  | 'done';

/**
 * Enough state to resume after a refresh.
 *
 * A kiosk browser reloads — a stray gesture, a crash, a patient handing the tablet back. The
 * interview lives on the server, so losing the client's `sessionRef` was the only thing
 * standing between the patient and their answers, and it sent them back to the language
 * picker with everything apparently gone.
 *
 * sessionStorage, not localStorage: this must not outlive the tab. Only the session
 * reference and the consent scopes are kept — never a clinical answer.
 */
const RESUME_KEY = 'medikiosk.resume';

interface Resume {
  sessionRef: string;
  stage: Stage;
  scopes: string[];
  language: string;
  answered: number;
  documentCount: number;
  documentsDone: boolean;
}

function readResume(): Resume | null {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY);
    return raw ? (JSON.parse(raw) as Resume) : null;
  } catch {
    return null;
  }
}

function writeResume(state: Resume | null): void {
  try {
    if (state) sessionStorage.setItem(RESUME_KEY, JSON.stringify(state));
    else sessionStorage.removeItem(RESUME_KEY);
  } catch {
    /* private browsing, or storage disabled — resume is a convenience, not a requirement */
  }
}

export function KioskApp(): JSX.Element {
  const saved = useRef(readResume());
  const [stage, setStage] = useState<Stage>(saved.current ? 'interview' : 'language');
  const [language, setLanguage] = useState(saved.current?.language ?? 'en');
  const [sessionRef, setSessionRef] = useState<string | null>(saved.current?.sessionRef ?? null);
  const [step, setStep] = useState<StepResponse | null>(null);
  const [voice, setVoice] = useState<VoiceOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [answered, setAnswered] = useState(saved.current?.answered ?? 0);
  const [documentCount, setDocumentCount] = useState(saved.current?.documentCount ?? 0);
  /** Once the document step is behind us, finishing the interview again — which happens
   *  whenever the patient corrects an answer from the review screen — must return to review,
   *  not walk them back through the upload step. */
  const [documentsDone, setDocumentsDone] = useState(saved.current?.documentsDone ?? false);
  /** What the patient actually consented to. The kiosk must never offer a capture path they
   *  declined: the backend correctly refuses it with a 403, and a 403 the patient cannot act
   *  on is a worse experience than simply not offering the feature. */
  const [scopes, setScopes] = useState<string[]>(saved.current?.scopes ?? []);

  const canScanDocuments = scopes.includes('documents') && !documentsDone;

  const apply = useCallback(
    (response: StepResponse) => {
      setStep(response);
      setVoice(response.voice ?? null);
      // Skip the document stage when the patient declined that scope, or has already done
      // it. Review always runs: it is the last chance to catch a mishearing before a
      // physician sees it.
      if (response.complete) setStage(canScanDocuments ? 'documents' : 'review');
    },
    [canScanDocuments],
  );

  const guard = useCallback(
    async (work: () => Promise<StepResponse>) => {
      setBusy(true);
      setError(null);
      try {
        apply(await work());
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : 'Something went wrong. Please tell the staff.');
      } finally {
        setBusy(false);
      }
    },
    [apply],
  );

  useEffect(() => {
    if (stage !== 'interview' || !sessionRef || step) return;
    void guard(() => api.next(sessionRef));
  }, [stage, sessionRef, step, guard]);

  // Persist just enough to resume. Never a clinical answer — those live on the server.
  useEffect(() => {
    const resumable = sessionRef && stage !== 'language' && stage !== 'login' && stage !== 'done';
    writeResume(
      resumable
        ? { sessionRef, stage, scopes, language, answered, documentCount, documentsDone }
        : null,
    );
  }, [sessionRef, stage, scopes, language, answered, documentCount, documentsDone]);

  // A restored session must be re-validated: it may have expired or been purged while the
  // tab was closed. Trusting sessionStorage here would strand the patient on a dead screen.
  useEffect(() => {
    if (!saved.current) return;
    const ref = saved.current.sessionRef;
    saved.current = null;
    void api.sessionState(ref).catch(() => {
      writeResume(null);
      setSessionRef(null);
      setStep(null);
      setScopes([]);
      setStage('language');
      setError('Your previous session has ended. Please start again.');
    });
  }, []);

  function restart(): void {
    writeResume(null);
    setToken(null);
    setSessionRef(null);
    setStep(null);
    setVoice(null);
    setAnswered(0);
    setDocumentCount(0);
    setDocumentsDone(false);
    setScopes([]);
    setError(null);
    setStage('language');
  }

  const question = step?.question ?? null;

  return (
    // Every browser refuses audio until the user has interacted with the page, and the
    // kiosk's first prompt speaks by itself as soon as a question renders. Priming the
    // speech engine from the first tap anywhere — a language, a consent toggle, a keypad
    // digit — is what makes that prompt audible. `unlock()` is idempotent and inaudible.
    <KioskShell
      onPointerDownCapture={() => unlock()}
      progress={
        stage === 'interview' && step ? (
          <ProgressRail
            progress={step.progress}
            sections={step.sections}
            currentSectionId={question?.sectionId ?? null}
          />
        ) : undefined
      }
      actions={
        stage !== 'language' ? (
          <Button variant="quiet" size="sm" onClick={restart}>
            Start over
          </Button>
        ) : undefined
      }
    >
      <>
        {error && <div className="kiosk-error">{error}</div>}

        {stage === 'language' && (
          <LanguagePicker
            onPick={(picked) => {
              setLanguage(picked);
              setStage('login');
            }}
          />
        )}

        {stage === 'login' && (
          <AbhaLogin onAuthenticated={() => setStage('memory')} onBack={() => setStage('language')} />
        )}

        {stage === 'memory' && (
          <PatientHome
            onStartVisit={() => setStage('consent')}
            onBack={() => setStage('login')}
          />
        )}

        {stage === 'consent' && (
          <ConsentGate
            language={language}
            onGranted={(ref, _ayushMode, grantedScopes) => {
              setSessionRef(ref);
              setScopes(grantedScopes);
              setStage('interview');
            }}
            onBack={() => setStage('login')}
          />
        )}

        {stage === 'interview' && question && sessionRef && (
          <>
            {/* Reachable from every question, never blocking one. It sits above the
                Back row as a quiet chip rather than a button, because it is an
                aside — the question is the task. */}
            <div className="kx-records-slot">
              <Chip
                active={documentCount > 0}
                icon={<Icon name="camera" />}
                onClick={() => setStage('documents')}
              >
                {documentCount
                  ? `${documentCount} record${documentCount === 1 ? '' : 's'} added — add another`
                  : 'Add a prescription or report'}
              </Chip>
            </div>
            <QuestionCard
            question={question}
            sessionRef={sessionRef}
            voice={voice}
            busy={busy}
            voiceEnabled={scopes.includes('voice')}
            reopened={Boolean(step?.reopened)}
            currentAnswer={step?.currentAnswer ?? null}
            canGoBack={step?.canGoBack !== false}
            onBack={() => void guard(() => api.back(sessionRef))}
            onAnswer={(value) => {
              setAnswered((n) => n + 1);
              void guard(() => api.answer(sessionRef, question.turnId, question.questionId, value));
            }}
            onTyped={(value) => {
              setAnswered((n) => n + 1);
              void guard(() => api.answerTyped(sessionRef, question.turnId, question.questionId, value));
            }}
            onSpoken={(transcript, confidence, bargeIn) => {
              void guard(() =>
                api.answerVoice(
                  sessionRef,
                  question.turnId,
                  question.questionId,
                  transcript,
                  confidence,
                  bargeIn,
                ),
              );
            }}
            onSkip={() => void guard(() => api.skip(sessionRef, question.questionId))}
            />
          </>
        )}

        {stage === 'interview' && !question && !busy && (
          <div className="kiosk-panel">
            <p className="kiosk-lead">Loading your first question…</p>
          </div>
        )}

        {stage === 'documents' && sessionRef && (
          <DocumentUpload
            sessionRef={sessionRef}
            alreadyUploaded={documentCount}
            consented={scopes.includes('documents')}
            onGrantConsent={async () => {
              // The patient declined this scope earlier and has now asked to use it. Ask for
              // that one permission, in place, at the moment it is needed.
              await api.grantScope(sessionRef, 'documents');
              setScopes((current) => [...current, 'documents']);
            }}
            onDone={(uploaded) => {
              setDocumentCount(uploaded);
              // Only the end-of-interview visit closes the step; an upload the patient
              // started mid-interview returns them to the question they left.
              if (step?.complete) {
                setDocumentsDone(true);
                setStage('review');
              } else {
                setStage('interview');
              }
            }}
          />
        )}

        {stage === 'review' && sessionRef && (
          <PatientReview
            sessionRef={sessionRef}
            onCorrect={(questionId) => {
              setStage('interview');
              void guard(() => api.reopen(sessionRef, questionId));
            }}
            onConfirm={() => setStage('done')}
          />
        )}

        {stage === 'done' && (
          <DoneScreen
            language={language}
            answered={answered}
            documents={documentCount}
            onRestart={restart}
          />
        )}
      </>
    </KioskShell>
  );
}
