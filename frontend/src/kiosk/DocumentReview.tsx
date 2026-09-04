/**
 * "This is what I read off your paper. Is it right?"
 *
 * OCR must never silently become truth, and the patient holding the prescription is the
 * cheapest, best-informed check available — long before a physician sees it.
 *
 * The two lanes are treated differently on purpose:
 *
 *  - A **low-confidence** item was never recorded. Confirming it is what admits it, so the
 *    buttons read as a decision the patient is making.
 *  - A **high-confidence** item is already recorded as document-tier: it is what the paper
 *    *says*. A patient disagreeing does not delete it, because the paper still says what it
 *    says. It is flagged for the physician instead. That is not a technicality — "the
 *    prescription says metformin but the patient says they never took it" is a real and
 *    clinically important state, and collapsing it to either side loses information.
 *
 * THREE ACTIONS, NOT TWO. "Yes / No" cannot express the commonest outcome of reading a photo:
 * the right medicine, misread. A real upload in testing produced "AMLODIPINE SMG" from a page
 * that says 5MG. Under a two-way choice the patient must either confirm something wrong or
 * reject something right, and either answer loses the truth. So: Confirm, Correct (type what
 * the paper actually says), Discard.
 *
 * UNCERTAIN AND UNKNOWN ARE DIFFERENT THINGS, and they are rendered differently. A LOW
 * confidence is the engine saying "I read this badly" — a statement about the reading. A NULL
 * confidence is the engine saying nothing at all, because it did not report one — a statement
 * about the engine. Showing both as "not clear" tells the patient the reading is doubtful when
 * what is actually true is that nobody knows. Both default to unconfirmed, because neither is
 * evidence of correctness, but they must not look the same.
 */
import { useState } from 'react';
import { ApiError, api, type ExtractedItem, type OcrQuality, type OcrRegion } from '../shared/api';
import { Icon } from '../shared/Icon';
import { SourceCrop } from './SourceCrop';
import { DocumentRegions, RegionDetail } from './DocumentRegions';

interface Props {
  sessionRef: string;
  documentId: string;
  filename: string;
  /** prescription | lab_report | discharge_summary | other, from what was found on it. */
  kind: string;
  items: ExtractedItem[];
  /** Every region OCR detected, whether or not an entity came out of it. Optional so a
   *  document read by an engine that does not segment renders exactly as it did before. */
  regions?: OcrRegion[];
  quality?: OcrQuality | null;
  onDone: () => void;
}

/** Medicines and results are what a patient can meaningfully check. Dates and headings are
 *  extraction plumbing, and asking about them buys nothing but fatigue. */
const CHECKABLE = new Set(['medication', 'investigation', 'diagnosis']);

/** What to call the document to the patient. "discharge_summary" is not a phrase. */
const KIND_LABEL: Record<string, string> = {
  prescription: 'prescription',
  lab_report: 'test report',
  discharge_summary: 'hospital paper',
  other: 'paper',
};

export function DocumentReview({
  sessionRef,
  documentId,
  filename,
  regions = [],
  quality = null,
  kind,
  items,
  onDone,
}: Props): JSX.Element {
  const [decided, setDecided] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Which OCR region is highlighted. Set by clicking a box on the page OR by clicking a
   *  reading in the list — the two are the same selection, which is what makes the link
   *  legible in both directions. */
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(null);
  /** itemId -> the text the patient is typing, while a row is being corrected. */
  const [editing, setEditing] = useState<Record<string, string>>({});

  const checkable = items.filter((item) => CHECKABLE.has(item.kind));

  async function decide(
    item: ExtractedItem,
    action: 'confirm' | 'correct' | 'dispute',
    correctedText?: string,
  ): Promise<void> {
    setBusy(item.itemId);
    setError(null);
    try {
      await api.reviewDocumentItem(sessionRef, documentId, {
        itemId: item.itemId,
        action,
        ...(correctedText ? { correctedText } : {}),
      });
      setDecided((current) => ({ ...current, [item.itemId]: action }));
      setEditing((current) => {
        const next = { ...current };
        delete next[item.itemId];
        return next;
      });
    } catch (exc) {
      setError(
        exc instanceof ApiError ? exc.message : 'Could not save that. Please try again.',
      );
    } finally {
      setBusy(null);
    }
  }

  if (!checkable.length) {
    return (
      <div className="kiosk-panel">
        <h1 className="kiosk-title">We could not read that paper</h1>
        <p className="kiosk-lead">
          Nothing could be read from {filename}. The doctor will still see the picture you
          took, so nothing is lost. You can try another photo if you like.
        </p>
        <div className="kiosk-actions">
          <button type="button" className="btn-primary" onClick={onDone}>
            Continue
          </button>
        </div>
      </div>
    );
  }

  const pending = checkable.filter((item) => item.pending && !decided[item.itemId]);
  const selectedRegion = regions.find((region) => region.regionId === selectedRegionId) ?? null;

  return (
    <div className="kiosk-panel">
      <h1 className="kiosk-title">Is this right?</h1>
      <p className="kiosk-lead">
        This is what we read from your {KIND_LABEL[kind] ?? 'paper'}. Please check the
        medicines and results.
      </p>

      {error && <div className="kiosk-error">{error}</div>}

      {/* WHAT WAS FOUND, AS COUNTS. A page that is partly readable is the normal outcome on
          handwriting, and saying so beats both "success" and "we could not read that". */}
      {quality && quality.regionsDetected > 0 && (
        <p className="ocr-summary">
          {quality.regionsDetected} lines of handwriting found ·{' '}
          <strong>{quality.regionsRecognised}</strong> read clearly ·{' '}
          <strong>{quality.regionsNeedingReview}</strong> need checking
          {quality.regionsUnreadable > 0 && (
            <>
              {' '}
              · <strong>{quality.regionsUnreadable}</strong> could not be read
            </>
          )}
        </p>
      )}

      {regions.length > 0 && (
        <div className="ocr-evidence">
          <DocumentRegions
            pageUrl={api.sessionDocumentFileUrl(sessionRef, documentId, 1)}
            regions={regions}
            selectedRegionId={selectedRegionId}
            onSelect={(region) => setSelectedRegionId(region?.regionId ?? null)}
          />
          {selectedRegion && <RegionDetail region={selectedRegion} />}
        </div>
      )}

      <div className="extract-list">
        {checkable.map((item) => {
          const outcome = decided[item.itemId];
          const certainty = certaintyOf(item);
          const isEditing = item.itemId in editing;
          return (
            <div
              key={item.itemId}
              className={`extract-item extract-item--${certainty.tone}${
                outcome ? ` decided ${outcome}` : ''
              }${item.regionId != null && item.regionId === selectedRegionId ? ' is-linked' : ''}`}
              data-certainty={certainty.tone}
              // Selecting a reading highlights the exact strip of the page it was read from
              // and scrolls it into view. The reverse — clicking a box — is handled by the
              // map above; both write the same piece of state.
              onMouseEnter={() =>
                item.regionId != null && setSelectedRegionId(item.regionId)
              }
              onFocusCapture={() =>
                item.regionId != null && setSelectedRegionId(item.regionId)
              }
            >
              {/* The patch of their own paper this came from. It is what turns "do you
                  remember?" into "do these match?", which is a question a patient can
                  actually answer — including one who cannot read the text. */}
              <SourceCrop
                pageUrl={api.sessionDocumentFileUrl(sessionRef, documentId, item.page)}
                bbox={item.bbox}
                label={item.text}
              />

              <div className="extract-body">
                <div className="extract-name">{item.text}</div>
                <div className="extract-detail">{describe(item)}</div>
                <div className={`extract-band extract-band--${certainty.tone}`}>
                  <Icon name={certainty.glyph} />
                  {certainty.label}
                </div>
              </div>

              <div className="extract-actions">
                {outcome ? (
                  <span className={`extract-outcome ${outcome}`}>{OUTCOME_LABEL[outcome]}</span>
                ) : isEditing ? (
                  <div className="extract-correct">
                    <label htmlFor={`fix-${item.itemId}`}>What does the paper say?</label>
                    <input
                      id={`fix-${item.itemId}`}
                      value={editing[item.itemId]}
                      autoFocus
                      onChange={(event) =>
                        setEditing((current) => ({
                          ...current,
                          [item.itemId]: event.target.value,
                        }))
                      }
                    />
                    <div className="extract-correct__actions">
                      <button
                        type="button"
                        className="btn-small primary"
                        disabled={busy === item.itemId || !editing[item.itemId].trim()}
                        onClick={() =>
                          void decide(item, 'correct', editing[item.itemId].trim())
                        }
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="btn-small"
                        onClick={() =>
                          setEditing((current) => {
                            const next = { ...current };
                            delete next[item.itemId];
                            return next;
                          })
                        }
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <button
                      type="button"
                      className="btn-small primary"
                      disabled={busy === item.itemId}
                      onClick={() => void decide(item, 'confirm')}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="btn-small"
                      disabled={busy === item.itemId}
                      onClick={() =>
                        setEditing((current) => ({ ...current, [item.itemId]: item.text }))
                      }
                    >
                      Correct
                    </button>
                    <button
                      type="button"
                      className="btn-small btn-discard"
                      disabled={busy === item.itemId}
                      onClick={() => void decide(item, 'dispute')}
                    >
                      Discard
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {pending.length > 0 && (
        <p className="kiosk-help" style={{ color: 'var(--warn)', marginTop: 16 }}>
          {pending.length === 1
            ? 'One item still needs checking. Anything you do not check is left for the doctor.'
            : `${pending.length} items still need checking. Anything you do not check is left for the doctor.`}
        </p>
      )}

      <div className="kiosk-actions">
        <button type="button" className="btn-primary" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

const OUTCOME_LABEL: Record<string, string> = {
  confirm: 'Confirmed',
  correct: 'Corrected',
  dispute: 'Left for the doctor',
};

/**
 * How sure the engine was — and, separately, whether it said.
 *
 * `confidence: null` is NOT low confidence. It means the backend reported none, which is a
 * fact about the engine rather than about the reading. A patient told "not clear" about a
 * perfectly clear line learns something false; a patient told "we cannot tell how well this
 * was read" learns something true. Both stay unconfirmed, because neither is evidence of
 * correctness — but they get different words, different glyphs and different tints.
 */
function certaintyOf(item: ExtractedItem): {
  tone: 'clear' | 'unclear' | 'unknown';
  label: string;
  glyph: 'check' | 'other';
} {
  const raw = item.confidence as number | null | undefined;
  if (raw === null || raw === undefined || Number.isNaN(raw)) {
    return {
      tone: 'unknown',
      label: 'We cannot tell how clearly this was read — please check it',
      glyph: 'other',
    };
  }
  if (item.confidenceBand === 'verify') {
    return { tone: 'unclear', label: 'Not clear — please check this one', glyph: 'other' };
  }
  return { tone: 'clear', label: 'Read clearly', glyph: 'check' };
}

/** Dose and frequency in the patient's words, not the extractor's field names. */
function describe(item: ExtractedItem): string {
  const detail = item.detail ?? {};
  const parts = [detail.dose, detail.frequencyRaw ?? detail.frequency, detail.duration]
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0);
  if (parts.length) return parts.join(' · ');
  if (item.kind === 'investigation' && detail.value != null) {
    return `${detail.value}${detail.unit ? ` ${detail.unit}` : ''}`;
  }
  return item.sourceText;
}
