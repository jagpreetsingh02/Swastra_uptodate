/**
 * The single place the frontend talks to the backend.
 *
 * Every response the API sends is camelCase (the backend applies that at its boundary), so
 * nothing here renames anything. If a field arrives snake_case, that is a backend bug, not a
 * mapping to add here — a translation layer is where casing drift goes to hide.
 */

export type Tier = 'stated' | 'confirmed' | 'document';
export type Priority = 'routine' | 'urgent' | 'immediate';

export interface Option {
  value: string;
  label: string;
  labelEn: string;
  icon: string | null;
  exclusive: boolean;
}

export interface Scale {
  min: number;
  max: number;
  faces: boolean;
  anchors_en: string[];
  anchors_hi: string[];
}

export interface Question {
  turnId: string;
  questionId: string;
  path: string;
  kind: 'open_text' | 'single_choice' | 'multi_choice' | 'boolean' | 'scale' | 'duration' | 'derived';
  prompt: string;
  help: string | null;
  language: string;
  translationMissing: boolean;
  sectionId: string;
  sectionTitle: string;
  socrates: string | null;
  options: Option[];
  scale: Scale | null;
  required: boolean;
  touchOnly: boolean;
  progress: Progress;
}

export interface Progress {
  answered: number;
  askable: number;
  percent: number;
  sections: number;
}

export interface SectionProgress {
  sectionId: string;
  title: string;
  answered: number;
  total: number;
  complete: boolean;
}

export interface StepResponse {
  complete: boolean;
  question: Question | null;
  progress: Progress;
  sections: SectionProgress[];
  voice?: VoiceOutcome;
  escalation?: Escalation;
  recorded?: { factId: string; path: string; tier: Tier }[];
  /** True when this question is being corrected rather than asked for the first time. */
  reopened?: boolean | string;
  /** The answer already on file for a reopened question, so the kiosk can pre-fill it. */
  currentAnswer?: { value: unknown; verbatim: string | null; declined: boolean } | null;
  /** False on the first question, where Back has nowhere to go. */
  canGoBack?: boolean;
}

export interface VoiceOutcome {
  accepted: boolean;
  degradedToTouch: boolean;
  reason: 'unclear' | 'silence' | null;
  transcript: {
    text: string;
    confidence: number | null;
    confidenceStatus: 'measured' | 'unavailable';
    reliable: boolean;
    threshold: number;
  };
  factsRecorded: number;
  prompt: string | null;
}

export interface RedFlag {
  ruleId: string;
  label: string;
  level: 'urgent' | 'immediate';
  rationale: string;
  triggeringFactIds: string[];
}

export interface Escalation {
  priority: Priority;
  flags: RedFlag[];
  immediateCount: number;
  urgentCount: number;
}

export interface ConsentScope {
  id: string;
  required: boolean;
  title: string;
  /** The compact label for the consent screen; falls back to `title` when absent. */
  short?: string;
  audio: string;
}

export interface ConsentPresentation {
  policyVersion: string;
  preamble: string;
  scopes: ConsentScope[];
}

export interface Source {
  factId: string;
  tier: Tier;
  confidence: number;
  verbatim: string;
  language: string;
  kind: 'utterance' | 'document';
  questionId: string | null;
  modality: string | null;
  asrConfidence: number | null;
  documentId: string | null;
  page: number | null;
  bbox: { x: number; y: number; width: number; height: number } | null;
  handwritten: boolean | null;
}

export interface SummaryLine {
  sectionId: string;
  text: string;
  kind: 'fact' | 'structural';
  emphasis: 'immediate' | 'urgent' | 'unverified' | null;
  sources: Source[];
}

export interface SummarySection {
  sectionId: string;
  title: string;
  lines: { text: string; factIds: string[]; kind: string; tier: string | null; emphasis: string | null }[];
}

export interface Summary {
  sessionId: string;
  generatedAt: string;
  status: string;
  completeness: number;
  sections: SummarySection[];
  warnings: string[];
  notice: string;
  traceability: { ok: boolean; factLines: number; untracedLines: string[]; unsupportedTokens: unknown[] };
  lines: SummaryLine[];
  escalation: Escalation;
  history: History;
}

export interface History {
  sessionId: string;
  contradictions: Contradiction[];
  demographics: { abhaRef: string | null; ageYears: number | null; gender: string | null; language: string };
  documents: { documentId: string; filename: string; pages: number; ocrBackend: string; meanConfidence: number; lowConfidencePages: number[] }[];
  medications: { entryId: string; name: Slot; dose: Slot; frequency: Slot; coding: Coding | null }[];
  problems: { entryId: string; reportedTerm: Slot; coding: Coding | null; unmapped: boolean }[];
  declined: string[];
  notAsked: string[];
  overallCompleteness: number;
}

export interface Slot {
  path: string;
  label: string;
  value: unknown;
  status: 'recorded' | 'not_asked' | 'declined';
  tier: Tier | null;
  confidence: number | null;
  factIds: string[];
  verbatim: string | null;
  superseded: { value: unknown; verbatim: string; recordedAt: string; factId: string }[];
}

export interface Coding {
  system: string;
  version: string;
  code: string;
  display: string;
}

export interface DemoCase {
  id: string;
  title: string;
  shows: string;
  language: string;
  ayush: boolean;
  document: string | null;
  watchFor: string[];
}

export interface DemoLoadResult {
  case: DemoCase;
  sessionRef: string;
  answered: number;
  spokenTurns: number;
  degradedToTouch: number;
  factsRecorded: number;
  priority: Priority;
  redFlags: string[];
  contradictions: number;
  document: { documentId: string; factsRecorded: number; needsVerification: number } | null;
}

export interface Contradiction {
  contradictionId: string;
  ruleId: string;
  label: string;
  patientSide: ContradictionSide;
  documentSide: ContradictionSide;
  clarifyingQuestion: string | null;
  status: string;
}

export interface ContradictionSide {
  factId: string;
  path: string;
  value: unknown;
  tier: Tier;
  verbatim: string;
  confidence: number;
  origin: string;
}

export interface ReviewAnswer {
  questionId: string;
  sectionTitle: string;
  question: string;
  answer: string;
  tier: Tier;
  canCorrect: boolean;
}

export interface ExtractedItem {
  itemId: string;
  kind: string;
  text: string;
  page: number;
  confidence: number;
  /** Coarse on purpose — a patient reading "81%" hears "81% likely to be the right medicine". */
  confidenceBand: 'high' | 'medium' | 'verify';
  pending: boolean;
  handwritten: boolean;
  sourceText: string;
  /** Normalised page coordinates, origin top-left, each in [0, 1]. */
  bbox: { x: number; y: number; width: number; height: number };
  detail: Record<string, string | number | null | undefined>;
  observedOn: string | null;
  entityIndex?: number;
  /** Which detected OCR region produced this. Null for engines that do not segment. */
  regionId?: number | null;
  /** False when the recogniser exposed no trustworthy score for the region. */
  confidenceMeasured?: boolean;
  patientReview?: 'confirm' | 'correct' | 'dispute';
  patientReading?: string;
  patientDisputed?: boolean;
}

export interface SessionDocument {
  documentId: string;
  filename: string;
  mediaType: string;
  pages: number;
  backend: string;
  meanConfidence: number;
  needsVerification: boolean;
  verifiedBy: string | null;
  kind: string;
  extracted: ExtractedItem[];
  ocrRegions?: OcrRegion[];
  quality?: OcrQuality | null;
}

/** One detected OCR region: a strip of the page, what was read from it, and how sure. */
export interface OcrRegion {
  regionId: number;
  documentId: string;
  page: number;
  /** Normalised against the PREPARED page — the same convention `SourceCrop` uses, and the
   *  same image `sessionDocumentFileUrl` serves. Origin top-left, each in [0, 1]. */
  bbox: { x: number; y: number; width: number; height: number };
  text: string;
  /** NULL means the recogniser exposed no trustworthy score. It does not mean zero, and the
   *  two must never render the same way. */
  confidence: number | null;
  confidenceBand: 'high' | 'medium' | 'verify' | 'unreadable';
  backend: string;
  cropWidth: number | null;
  cropHeight: number | null;
  failure: string | null;
  /** `itemId`s of the entities this region produced. The link runs both ways: an item stores
   *  `regionId`, a region lists its items. */
  itemIds: string[];
}

/** Counts rather than a verdict, so a partly-readable page is reported as one. */
export interface OcrQuality {
  regionsDetected: number;
  regionsRecognised: number;
  regionsNeedingReview: number;
  regionsUnreadable: number;
  originalLongEdge: number | null;
  underResolution: boolean;
  medianLineHeight: number | null;
}

export interface UploadResult {
  /** Every detected region, whether or not an entity came out of it. */
  ocrRegions?: OcrRegion[];
  quality?: OcrQuality | null;
  /** The page was below the resolution at which text can be resolved. Null when the source
   *  was not an image. More actionable than "nothing found", so the failure screen prefers it. */
  tooSmall?: boolean | null;
  documentId: string;
  filename: string;
  backend: string;
  meanConfidence: number;
  factsRecorded: number;
  lowConfidenceCount: number;
  documentKind: string;
  extracted: ExtractedItem[];
  needsVerification: {
    entityIndex: number;
    kind: string;
    text: string;
    confidence: number;
    sourceText: string;
    page: number;
  }[];
}

export interface PatientOverview {
  known: boolean;
  patientRef?: string;
  displayName?: string | null;
  abhaMasked?: string | null;
  ageYears?: number | null;
  gender?: string | null;
  counts: {
    encounters: number;
    prescriptions: number;
    labReports: number;
    otherDocuments?: number;
    medications?: number;
    observations?: number;
  };
  recent: {
    encounterRef: string;
    occurredOn: string;
    headline: string;
    priority: Priority;
    ayush: boolean;
  }[];
  note?: string;
}

export interface TimelineRow {
  eventRef: string;
  occurredOn: string | null;
  datePrecision: string;
  kind: string;
  label: string;
  detail: string | null;
  documentRef: string | null;
  factRef: string | null;
  lowConfidence: boolean;
  encounterRef: string | null;
}

export interface MedicationThread {
  name: string;
  normalized: string;
  needsReconciliation: boolean;
  reason: string | null;
  mentions: {
    status: string;
    dose: string | null;
    frequency: string | null;
    observedOn: string | null;
    recordedAt: string | null;
    documentRef: string | null;
    encounterRef: string | null;
    encounterOn: string | null;
    howWeKnow: string;
  }[];
}

export interface SimilarEncounter {
  encounterRef: string;
  occurredOn: string;
  headline: string | null;
  shared: { feature: string; value: string; path: string }[];
  sharedCount: number;
  band: string;
  note: string;
}

export interface ReconciliationFinding {
  kind: string;
  currentStatement: string;
  historicalEvidence: {
    name: string;
    mentions: MedicationThread['mentions'];
  }[];
  status: string;
  note: string;
}

/**
 * A live session joined to the person it belongs to. `known: false` is a normal answer —
 * a first-time patient at a walk-in OPD is the common case, not an error.
 */
export interface PatientContext {
  sessionRef: string;
  known: boolean;
  patientRef?: string;
  overview: PatientOverview | null;
  timeline: TimelineRow[];
  medications: MedicationThread[];
  similar: SimilarEncounter[];
  reconciliation: ReconciliationFinding[];
  currentFeatures?: { path: string; label: string; values: string[] }[];
  note?: string;
}


// ------------------------------------------------------- the clinical report

export interface LabPoint {
  observedOn: string | null;
  value: number | null;
  rangeFlag: string;
  documentRef: string | null;
}

export interface LabSeries {
  analyteKey: string;
  display: string;
  unit: string | null;
  referenceLow: number | null;
  referenceHigh: number | null;
  rangeSource: string;
  points: LabPoint[];
  latest: { value: number | null; observedOn: string | null; rangeFlag: string };
  /** Arithmetic between the last two measurements. Never a projection. */
  change: {
    delta: number;
    direction: 'higher' | 'lower' | 'unchanged';
    sinceOn: string | null;
    sinceValue: number | null;
  };
  outOfRangeCount: number;
}

export interface ClinicalReport {
  patientRef: string;
  displayName: string | null;
  generatedAt: string;
  current: {
    encounterRef: string;
    occurredOn: string;
    headline: string | null;
    priority: Priority;
    completeness: number;
    confirmedBy: string;
    factCount: number;
  } | null;
  trends: LabSeries[];
  medications: {
    count: number;
    needsReconciliation: string[];
    threads: MedicationThread[];
    note: string;
  };
  recurrence: {
    visits: number;
    firstSeenOn: string | null;
    groups: { headline: string | null; count: number; occurredOn: string[]; encounterRefs: string[] }[];
    note: string;
  };
  redFlags: {
    evaluated: number;
    fired: { ruleId: string; level: string | null; rationale: string | null; evidence: unknown }[];
    note: string;
  };
  changed: {
    comparedWith: { encounterRef: string; occurredOn: string; headline: string | null } | null;
    new: { path: string; value: string }[];
    resolved: { path: string; value: string }[];
    persisting: { path: string; value: string }[];
    note?: string;
  };
  counts: { encounters: number; observations: number; medicationEvents: number };
  notice: string;
}

export interface Inspect {
  sessionRef: string;
  stateMachine: {
    currentNode: string; currentSection: string | null; turnsTaken: number;
    askable: number; declined: number; degradedToTouch: number; note: string;
  };
  facts: {
    active: number; superseded: number; byTier: Record<string, number>;
    withoutSource: number; absences: number;
  };
  redFlags: { rulesEvaluated: number; fired: string[]; priority: string; note: string };
  contradictions: number;
  consent: { scopes: string[]; ref: string | null };
  backends: {
    llm: { name: string; offline: boolean };
    speech: { name: string; offline: boolean };
    ocr: string;
  };
  audit: { intact: boolean; events: number };
  inspectLatencyMs: number;
}

export interface QueueEntry {
  sessionRef: string;
  priority: Priority;
  status: string;
  language: string;
  ayushMode: boolean;
  createdAt: string;
  waitingMinutes: number;
}

export interface TimelinePeriod {
  period: string;
  label: string;
  events: {
    eventId: string;
    occurredOn: string | null;
    datePrecision: string;
    kind: string;
    label: string;
    detail: string | null;
    lowConfidence: boolean;
    factIds: string[];
  }[];
}

let token: string | null = sessionStorage.getItem('medikiosk.token');

export function setToken(next: string | null): void {
  token = next;
  if (next) sessionStorage.setItem('medikiosk.token', next);
  else sessionStorage.removeItem('medikiosk.token');
}

export function getToken(): string | null {
  return token;
}

/**
 * An API failure with a message a patient can read.
 *
 * `message` is ALWAYS human. `detail` carries the technical cause for the physician
 * surface and the jury drawer. Nothing renders `detail` to a patient — "Request
 * failed (500)" and "TypeError: Failed to fetch" are exactly the console-like text
 * the product rules forbid on a clinical screen.
 */

// ──────────────────────────────────────── the Clinical Intelligence Brief
//
// Mirrors `app/modules/report/brief.py`. Deliberately structural rather than loose: every
// clinical line carries the refs click-to-source needs, and typing them means a section that
// forgets to pass them through fails at compile time rather than as a dead click.

export interface BriefLine {
  label: string;
  path: string;
  value: unknown;
  displayValue: string | null;
  state: string;
  tier: string;
  confidence: number | null;
  confidenceStatus: string;
  confirmedByPhysician: boolean;
  /** The handle that opens the original. No evidence -> the backend never sent the line. */
  factRef: string;
  evidenceIds: number[];
  evidenceKinds: string[];
  /** `document` | `voice` | `touch` | `typed` — what the drawer actually renders on. */
  evidenceModalities: string[];
}

export interface BriefEvidence {
  sourceType: string;
  verbatim: string;
  language: string;
  modality: string | null;
  questionId: string | null;
  asrConfidence: number | null;
  documentRef: string | null;
  page: number | null;
  bbox: { x: number; y: number; width: number; height: number } | null;
  ocrConfidence: number | null;
  handwritten: boolean;
  humanReading: string | null;
  readBy: string | null;
}

export interface FactEvidence {
  factRef: string;
  path: string;
  value: unknown;
  displayValue: string | null;
  tier: string;
  confidence: number | null;
  confidenceStatus: string;
  confirmedByPhysician: boolean;
  evidence: BriefEvidence[];
}

export interface ChangedItem {
  path: string;
  value: string;
  factRef?: string | null;
}

export interface LabPoint {
  observedOn: string | null;
  value: number | null;
  unit: string | null;
  rangeFlag: string;
  referenceLow: number | null;
  referenceHigh: number | null;
  rangeSource: string;
  documentRef: string | null;
}

export interface LabSeries {
  analyteKey: string;
  display: string;
  unit: string | null;
  points: LabPoint[];
  chartable: boolean;
  delta?: number | null;
  notChartableBecause?: string;
}

export interface Brief {
  reportVersion: string;
  audience: string;
  header: {
    patientRef: string;
    displayName: string | null;
    ageYears: number | null;
    gender: string | null;
    preferredLanguage: string;
    encounter: {
      encounterRef: string;
      occurredOn: string;
      headline: string | null;
      priority: string;
      language: string;
      ayushMode: boolean;
      consentRef: string | null;
    } | null;
    encounterCount: number;
  };
  snapshot: {
    items: BriefLine[];
    allergies: BriefLine[];
    reportedMedications: {
      name: string | null;
      dose: string | null;
      frequency: string | null;
      duration: string | null;
      lines: BriefLine[];
    }[];
    emptyReason: string | null;
  };
  redFlags: {
    items: { ruleId: string; level: string | null; rationale: string | null; evidence: unknown[] }[];
    note: string;
    emptyReason: string | null;
  };
  whatChanged: {
    comparedWith: { encounterRef: string; occurredOn: string; headline: string | null } | null;
    new: ChangedItem[];
    resolved: ChangedItem[];
    persisting: ChangedItem[];
    note?: string;
    emptyReason: string | null;
  };
  timeline: {
    items: {
      encounterRef: string;
      occurredOn: string;
      headline: string | null;
      priority: string;
      confirmedBy: string;
      isCurrent: boolean;
    }[];
    emptyReason: string | null;
  };
  medications: {
    items: {
      name: string;
      normalizedName: string | null;
      dose: string | null;
      frequency: string | null;
      duration: string | null;
      route: string | null;
      status: string;
      observedOn: string | null;
      documentRef: string | null;
      factRef: string | null;
      origin: string;
    }[];
    note: string;
    emptyReason: string | null;
  };
  observations: { series: LabSeries[]; singles: LabSeries[]; note: string; emptyReason: string | null };
  similarEncounters: {
    items: {
      encounterRef: string;
      occurredOn: string;
      headline: string | null;
      sharedFeatures: { path: string; value: string }[];
      why: string;
    }[];
    note: string;
    emptyReason: string | null;
  };
  contradictions: {
    items: {
      contradictionRef: string;
      ruleId: string;
      label: string;
      sideA: Record<string, unknown> | null;
      sideB: Record<string, unknown> | null;
    }[];
    note: string;
    emptyReason: string | null;
  };
  unresolved: {
    declinedOrUnknown: { path: string; label: string; state: string; factRef: string }[];
    superseded: { path: string; wasValue: unknown; factRef: string; recordedAt: string }[];
    invalidated: { path: string; wasValue: unknown; reason: string | null; factRef: string }[];
    note: string;
    emptyReason: string | null;
  };
  completeness: { collected: string[]; declined: string[]; missing: string[]; note: string };
  confirmation: {
    confirmed: boolean;
    confirmedBy: string | null;
    confirmedAt: string | null;
    decisions: { decision: string; actor: string; decidedAt: string }[];
    note: string;
  };
  notice: string;
}

export interface PatientBrief {
  reportVersion: string;
  audience: string;
  forWhom: string | null;
  groups: { title: string; items: { label: string; value: unknown }[]; emptyReason: string | null }[];
  notice: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly issueCode?: string,
    /** The raw cause. For diagnostics, never for the kiosk. */
    readonly detail?: string,
    /**
     * A STABLE MACHINE CODE for what went wrong — `too_large`, `unreadable_image`,
     * `heic_unreadable`, `unsupported_type`, `consent_required`.
     *
     * The failure screen has to tell these apart to offer the right next step: Retake fixes a
     * blurry photo and does nothing for an unsupported file type. Branching on the message
     * TEXT would work until someone improved the wording, which is the kind of coupling that
     * quietly stops anyone improving it.
     */
    readonly reason?: string,
  ) {
    super(message);
  }
}

/** `status: 0` — the request never reached the server at all. */
export const OFFLINE = 0;

/**
 * Render's free tier sleeps the backend after 15 minutes idle; the next request pays a
 * 30–60s cold boot. Nothing on screen distinguishes that from a hang or a crash — the ordinary
 * spinners were built assuming a warm server, so a patient staring at one for a minute reads it
 * as broken, not busy.
 *
 * This only ever fires for the FIRST request of a page load. `warmed` flips permanently true the
 * moment any response comes back (success or error) — a real 500 is not a cold start, and a
 * later slow request (OCR genuinely takes several seconds on a warm server) has its own honest
 * "Reading your paper…" copy already; re-showing a wake message over that would be dishonest in
 * the other direction, implying a sleep that did not happen.
 */
let warmed = false;
const WAKE_THRESHOLD_MS = 1800;
type WakeListener = (waking: boolean) => void;
const wakeListeners = new Set<WakeListener>();

export function subscribeToWakeState(fn: WakeListener): () => void {
  wakeListeners.add(fn);
  return () => wakeListeners.delete(fn);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let wakeTimer: ReturnType<typeof setTimeout> | null = null;
  if (!warmed) {
    wakeTimer = setTimeout(() => {
      wakeListeners.forEach((fn) => fn(true));
    }, WAKE_THRESHOLD_MS);
  }

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (cause) {
    if (wakeTimer) {
      clearTimeout(wakeTimer);
      warmed = true;
      wakeListeners.forEach((fn) => fn(false));
    }
    // The API is down, the network dropped, or the dev server is up without the
    // backend behind it. `fetch` rejects with a bare TypeError here, which used to
    // escape uncaught and surface to the patient as raw JS.
    throw new ApiError(
      'We cannot reach the health service right now. Please ask a staff member for help.',
      OFFLINE,
      'offline',
      cause instanceof Error ? cause.message : String(cause),
    );
  }
  if (wakeTimer) {
    clearTimeout(wakeTimer);
    warmed = true;
    wakeListeners.forEach((fn) => fn(false));
  }

  const text = await response.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // A proxy error page or an HTML 502 — anything that is not the JSON we expect.
    // Parsing it used to throw a SyntaxError from inside the client.
    if (!response.ok) {
      throw new ApiError(
        'Something went wrong at our end. Please ask a staff member for help.',
        response.status,
        'bad-response',
        text.slice(0, 200),
      );
    }
  }

  if (!response.ok) {
    // The backend returns a FHIR OperationOutcome for every domain error, and its
    // diagnostics are written to be read by a person — that is the whole point of the
    // choice, so they are surfaced verbatim when present.
    const issue = body?.issue?.[0];
    if (issue?.diagnostics) {
      throw new ApiError(
        issue.diagnostics,
        response.status,
        issue.code,
        issue.diagnostics,
        // Carried in `details.text` rather than in the sentence the patient reads.
        issue.details?.text,
      );
    }
    // No OperationOutcome. Say something true and useful instead of the status code.
    throw new ApiError(
      response.status >= 500
        ? 'Something went wrong at our end. Please ask a staff member for help.'
        : 'That did not work. Please try again, or ask a staff member for help.',
      response.status,
      'unexpected',
      `HTTP ${response.status} ${path}`,
    );
  }
  return body as T;
}

export const api = {
  about: () => request<Record<string, unknown>>('/about'),
  languages: () => request<{ languages: { code: string; name: string }[] }>('/api/v1/languages'),
  consentPresentation: (language: string) =>
    request<ConsentPresentation>(`/api/v1/consent/presentation?language=${language}`),

  requestOtp: (abhaAddress: string) =>
    request<{ txnId: string; demoOtp: string; otpSentTo: string }>('/mock-idp/abha/request-otp', {
      method: 'POST',
      body: JSON.stringify({ abha_address: abhaAddress }),
    }),
  verifyOtp: (abhaAddress: string, otp: string) =>
    request<{ access_token: string; abhaRef: string; demographics: Record<string, unknown> }>(
      '/mock-idp/abha/verify-otp',
      { method: 'POST', body: JSON.stringify({ abha_address: abhaAddress, otp }) },
    ),
  staffToken: (role: string, sub: string) =>
    request<{ access_token: string }>('/mock-idp/token', {
      method: 'POST',
      body: JSON.stringify({ role, sub }),
    }),

  createSession: (language: string, consentScopes: string[], audioExplained: boolean) =>
    request<{ sessionRef: string; consentRef: string; ayushMode: boolean; demographics: Record<string, unknown> | null }>(
      '/api/v1/sessions',
      { method: 'POST', body: JSON.stringify({ language, consentScopes, audioExplained }) },
    ),
  sessionState: (ref: string) => request<Record<string, unknown>>(`/api/v1/sessions/${ref}`),

  next: (ref: string) => request<StepResponse>(`/api/v1/sessions/${ref}/dialogue/next`),
  answer: (ref: string, turnId: string, questionId: string, value: unknown) =>
    request<StepResponse>(`/api/v1/sessions/${ref}/dialogue/answer`, {
      method: 'POST',
      body: JSON.stringify({ turnId, questionId, value, modality: 'touch' }),
    }),
  answerTyped: (ref: string, turnId: string, questionId: string, value: string) =>
    request<StepResponse>(`/api/v1/sessions/${ref}/dialogue/answer`, {
      method: 'POST',
      body: JSON.stringify({ turnId, questionId, value, modality: 'typed' }),
    }),
  answerVoice: (
    ref: string,
    turnId: string,
    questionId: string,
    transcript: string,
    confidence: number | null,
    bargeIn: boolean,
  ) =>
    request<StepResponse>(`/api/v1/sessions/${ref}/dialogue/answer/voice`, {
      method: 'POST',
      body: JSON.stringify({ turnId, questionId, transcript, confidence, bargeIn }),
    }),
  review: (ref: string) =>
    request<{ answers: ReviewAnswer[]; language: string }>(
      `/api/v1/sessions/${ref}/dialogue/review`,
    ),
  reopen: (ref: string, questionId: string) =>
    request<StepResponse & { reopened: string }>(`/api/v1/sessions/${ref}/dialogue/reopen`, {
      method: 'POST',
      body: JSON.stringify({ questionId }),
    }),
  /** Reopen the previous answered question. The old fact is superseded, never deleted. */
  back: (ref: string) =>
    request<StepResponse & { reopened: string }>(`/api/v1/sessions/${ref}/dialogue/back`, {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  skip: (ref: string, questionId: string) =>
    request<StepResponse>(`/api/v1/sessions/${ref}/dialogue/skip`, {
      method: 'POST',
      body: JSON.stringify({ questionId }),
    }),
  speak: (ref: string, text: string) =>
    request<{ audioBase64: string | null; clientFallback: boolean; backend: string }>(
      `/api/v1/sessions/${ref}/dialogue/speak`,
      { method: 'POST', body: JSON.stringify({ text }) },
    ),

  upload: (ref: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<UploadResult>(`/api/v1/sessions/${ref}/documents`, {
      method: 'POST',
      body: form,
    });
  },

  sessionDocuments: (ref: string) =>
    request<{ documents: SessionDocument[] }>(`/api/v1/sessions/${ref}/documents`),

  reviewDocumentItem: (
    ref: string,
    documentId: string,
    body: { itemId: string; action: string; correctedText?: string },
  ) =>
    request<{ itemId: string; action: string; disputed: boolean; factsRecorded: string[] }>(
      `/api/v1/sessions/${ref}/documents/${documentId}/review`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  timeline: (ref: string) =>
    request<{ documents: unknown[]; periods: TimelinePeriod[]; eventCount: number }>(
      `/api/v1/sessions/${ref}/documents/timeline`,
    ),
  verifyEntity: (ref: string, documentId: string, entityIndex: number, accepted: boolean, correctedText?: string) =>
    request<Record<string, unknown>>(`/api/v1/sessions/${ref}/documents/${documentId}/verify`, {
      method: 'POST',
      body: JSON.stringify({ entityIndex, accepted, correctedText }),
    }),

  queue: () => request<{ queue: QueueEntry[]; count: number }>('/api/v1/queue'),
  contradictions: (ref: string) =>
    request<{ count: number; contradictions: Contradiction[]; note: string }>(
      `/api/v1/sessions/${ref}/contradictions`,
    ),

  /**
   * Fetch an authenticated image and hand back an object URL.
   *
   * An `<img src>` cannot carry a bearer token, and every document route requires one — so
   * the evidence drawer pointed at the URL directly and got a 400 it rendered as "the
   * original file is not available", which is a lie about why. Fetching it and wrapping the
   * blob keeps the authorisation and the audit entry the route writes.
   *
   * The caller owns the returned URL and must revoke it.
   */
  fetchImage: async (path: string): Promise<string> => {
    const headers = new Headers();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const response = await fetch(path, { headers });
    if (!response.ok) {
      const text = await response.text();
      let detail = `Could not load ${path}`;
      try {
        detail = JSON.parse(text)?.issue?.[0]?.diagnostics ?? detail;
      } catch {
        /* the body was not an OperationOutcome; keep the generic message */
      }
      throw new ApiError(detail, response.status);
    }
    return URL.createObjectURL(await response.blob());
  },

  inspect: (ref: string) => request<Inspect>(`/api/v1/sessions/${ref}/inspect`),

  patientContext: (ref: string) =>
    request<PatientContext>(`/api/v1/sessions/${ref}/patient-context`),

  myRecord: () => request<PatientOverview>('/api/v1/patients/me'),
  clinicalReport: (patientRef: string) =>
    request<ClinicalReport>(`/api/v1/patients/${patientRef}/report`),
  patientOverview: (patientRef: string) =>
    request<PatientOverview>(`/api/v1/patients/${patientRef}`),
  patientTimeline: (patientRef: string, kinds?: string) =>
    request<{ count: number; events: TimelineRow[]; availableKinds: string[] }>(
      `/api/v1/patients/${patientRef}/timeline${kinds ? `?kinds=${kinds}` : ''}`,
    ),
  patientMedications: (patientRef: string) =>
    request<{ medications: MedicationThread[]; needsReconciliation: string[]; note: string }>(
      `/api/v1/patients/${patientRef}/medications`,
    ),
  encounterDetail: (patientRef: string, encounterRef: string) =>
    request<{
      encounterRef: string;
      occurredOn: string;
      headline: string | null;
      features: Record<string, string[]>;
      similar: SimilarEncounter[];
      summary: Record<string, unknown> | null;
    }>(`/api/v1/patients/${patientRef}/encounters/${encounterRef}`),
  /** `page` asks for a PNG of that page — the only form a bounding box can be drawn on. */
  documentFileUrl: (patientRef: string, documentRef: string, page?: number) =>
    `/api/v1/patients/${patientRef}/documents/${documentRef}/file` +
    (page ? `?page=${page}` : ''),

  sessionDocumentFileUrl: (sessionRef: string, documentId: string, page?: number) =>
    `/api/v1/sessions/${sessionRef}/documents/${documentId}/file` +
    (page ? `?page=${page}` : ''),

  /** A patient's own CONFIRMED visits, newest first. Only what a physician committed. */
  myEncounters: (patientRef: string) =>
    request<{
      patientRef: string;
      displayName: string | null;
      isSynthetic: boolean;
      encounters: {
        encounterRef: string;
        occurredOn: string;
        headline: string | null;
        confirmedBy: string;
        confirmedAt: string | null;
        language: string;
      }[];
      note: string;
    }>(`/api/v1/patients/${patientRef}/encounters`),

  /**
   * The auditor's screen for one encounter. Every call carries `purpose=RESEARCH` because
   * the auditor role is scoped to purposes [RESEARCH, STATISTICS] in policy.yaml, and the
   * purpose check defaults to TREATMENT when unspecified — an auditor calling without one
   * is refused for that reason alone, found the hard way once already.
   */
  auditReview: (encounterRef: string) =>
    request<{
      encounterRef: string;
      occurredOn: string;
      confirmedBy: string;
      consentRef: string | null;
      chain: {
        intact: boolean;
        eventsChecked: number;
        totalEvents: number;
        firstBrokenIndex: number | null;
        firstBrokenEventId: number | null;
        detail: string | null;
      };
      trail: {
        id: number;
        ts: string;
        actor: string;
        actorRole: string;
        purposeOfUse: string;
        action: string;
        outcome: string;
        modelName: string | null;
      }[];
      provenance: {
        totalFacts: number;
        withEvidence: number;
        withExplicitAbsence: number;
        offenders: { factRef: string; path: string; state: string }[];
        complete: boolean;
      };
      noAssessmentClaim: {
        clean: boolean;
        offenders: { field: string; path: string }[];
      };
    }>(`/api/v1/audit/encounters/${encounterRef}?purpose=RESEARCH`),

  auditTamperDemo: () =>
    request<{
      available: boolean;
      eventsInDemo: number;
      tamperedEventId: number | null;
      tamperedField: string | null;
      originalValue: string | null;
      corruptedValue: string | null;
      detected: boolean;
      firstBrokenIndex: number | null;
      note: string;
    }>('/api/v1/audit/tamper-demo?purpose=RESEARCH'),

  /** Account STUB. Always refuses, in the same words for every cause — see routes_account. */
  signIn: (identifier: string, password: string) =>
    request<{ ok: boolean }>('/api/v1/account/sign-in', {
      method: 'POST',
      body: JSON.stringify({ identifier, password }),
    }),
  register: (identifier: string, password: string) =>
    request<{ created: boolean; stub: boolean; message: string; reference: string }>(
      '/api/v1/account/register',
      { method: 'POST', body: JSON.stringify({ identifier, password }) },
    ),

  /** Guest mode: a real synthetic record with the full seeded history. No account. */
  startGuest: () =>
    request<{
      patientRef: string;
      displayName: string;
      isSynthetic: boolean;
      encounters: number;
      notice: string;
    }>('/api/v1/demo/guest', { method: 'POST' }),
  resetGuest: (patientRef: string) =>
    request<{
      patientRef: string;
      displayName: string;
      wasReset: boolean;
      identical?: boolean;
      countsBefore?: Record<string, number>;
      countsAfter?: Record<string, number>;
    }>(`/api/v1/demo/guest/${patientRef}/reset`, { method: 'POST' }),

  /** The deterministic brief. Two calls on unchanged data return identical bytes. */
  brief: (patientRef: string) => request<Brief>(`/api/v1/patients/${patientRef}/brief`),
  patientBrief: (patientRef: string, encounterRef?: string) =>
    request<PatientBrief>(
      `/api/v1/patients/${patientRef}/brief/patient`
      + (encounterRef ? `?encounter=${encounterRef}` : ''),
    ),
  /**
   * The brief as a PDF, rendered server-side. Returns a blob URL the caller must revoke.
   * A plain <a href> cannot carry the bearer token, so it is fetched like any other route.
   */
  briefPdf: async (
    patientRef: string,
    audience: 'clinician' | 'patient',
    encounterRef?: string,
  ): Promise<{ url: string; filename: string }> => {
    const headers = new Headers();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const response = await fetch(
      `/api/v1/patients/${patientRef}/brief.pdf?audience=${audience}`
      + (encounterRef ? `&encounter=${encounterRef}` : ''),
      { headers },
    );
    if (!response.ok) throw new ApiError('The report could not be prepared.', response.status, 'pdf');
    const disposition = response.headers.get('Content-Disposition') ?? '';
    const match = /filename="([^"]+)"/.exec(disposition);
    return { url: URL.createObjectURL(await response.blob()), filename: match?.[1] ?? 'medikiosk-report.pdf' };
  },

  /** Click-to-source: opens the original statement, voice segment or document region. */
  briefEvidence: (patientRef: string, encounterRef: string, factRef: string) =>
    request<FactEvidence>(
      `/api/v1/patients/${patientRef}/encounters/${encounterRef}/facts/${factRef}`,
    ),

  demoCases: () => request<{ cases: DemoCase[]; notice: string }>('/api/v1/demo/cases'),
  loadDemoCase: (caseId: string, sessionRef: string) =>
    request<DemoLoadResult>(`/api/v1/demo/cases/${caseId}/load`, {
      method: 'POST',
      body: JSON.stringify({ sessionRef }),
    }),
  summary: (ref: string, prose = false) =>
    request<Summary>(`/api/v1/sessions/${ref}/summary?prose=${prose}`),
  factDetail: (ref: string, factId: string) =>
    request<{ explanation: string; source: Record<string, unknown>; value: unknown; tier: Tier }>(
      `/api/v1/sessions/${ref}/facts/${factId}`,
    ),
  editFact: (ref: string, path: string, value: unknown, reason: string) =>
    request<Record<string, unknown>>(`/api/v1/sessions/${ref}/summary/edit`, {
      method: 'POST',
      body: JSON.stringify({ path, value, reason }),
    }),
  commit: (ref: string) =>
    request<{ committed: boolean; bundleId: string; entries: number; hisPush: { status: string; detail: string }; purge: Record<string, unknown> | null }>(
      `/api/v1/sessions/${ref}/commit`,
      { method: 'POST', body: JSON.stringify({ confirmed: true }) },
    ),
  grantScope: (ref: string, scope: string) =>
    request<{ granted: string[]; addedScope: string }>(
      `/api/v1/sessions/${ref}/consent/grant`,
      { method: 'POST', body: JSON.stringify({ scope }) },
    ),
  revokeConsent: (ref: string, scopes?: string[]) =>
    request<Record<string, unknown>>(`/api/v1/sessions/${ref}/consent/revoke`, {
      method: 'POST',
      body: JSON.stringify(scopes ? { scopes } : {}),
    }),
};
