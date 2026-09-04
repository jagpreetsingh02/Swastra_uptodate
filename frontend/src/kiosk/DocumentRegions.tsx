/**
 * The prescription with the OCR's own boxes drawn on it, and each one clickable.
 *
 * WHY THIS IS NOT DECORATION. The verification lane already showed a crop beside each reading
 * (`SourceCrop`), which answers "where did THIS line come from". It cannot answer the two
 * questions a doctor actually asks of a handwritten page: *what else is on it*, and *did the
 * machine even look at that bit*. A page where nine lines were detected and two produced a
 * medicine is not a page with two medicines on it — it is a page with seven more lines that
 * were read and not understood, and those have to be visible or the omission is invisible.
 *
 * THE BOXES ARE THE BACKEND'S. They come from `ocrRegions`, which is built from the same
 * `OCRBlock`s the facts were built from — not from a parallel description that could drift.
 * Their coordinates are normalised against the PREPARED page, and the image below is the
 * prepared page (`sessionDocumentFileUrl` serves exactly what OCR read, see `render.py`), so
 * a box always sits on the pixels the recognizer was given.
 *
 * THREE STATES, THREE TREATMENTS, because they are three different claims:
 *
 *   read       a reading the engine was confident about
 *   verify     a reading that needs a human — including one whose confidence is UNKNOWN,
 *              which is not the same as low and never renders as 0%
 *   unreadable there is writing here and nobody has read it. Still boxed: hiding it would
 *              tell the doctor the page held less than it does.
 *
 * The image is FETCHED, not linked: every document route needs a bearer token and an
 * `<img src>` cannot carry one, so pointing at the URL directly returns 403 and renders as a
 * broken image — which reads as "your document is gone" rather than "you are not authorised".
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, type OcrRegion } from '../shared/api';

interface Props {
  pageUrl: string;
  regions: OcrRegion[];
  /** The region to highlight, driven from outside — clicking a medicine selects its box. */
  selectedRegionId?: number | null;
  onSelect?: (region: OcrRegion | null) => void;
}

export function DocumentRegions({
  pageUrl,
  regions,
  selectedRegionId = null,
  onSelect,
}: Props): JSX.Element | null {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const selectedRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    api
      .fetchImage(pageUrl)
      .then((url) => live && setSrc(url))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [pageUrl]);

  // Scroll the selected box into view when the selection came from elsewhere — clicking a
  // medicine in a list below should bring its region onto the screen, not leave the doctor
  // hunting for a highlight they cannot see.
  useEffect(() => {
    if (selectedRegionId == null) return;
    selectedRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selectedRegionId]);

  const counts = useMemo(() => {
    const tally = { read: 0, verify: 0, unreadable: 0 };
    for (const region of regions) {
      if (region.confidenceBand === 'unreadable') tally.unreadable += 1;
      else if (region.confidenceBand === 'verify') tally.verify += 1;
      else tally.read += 1;
    }
    return tally;
  }, [regions]);

  if (!regions.length) return null;

  return (
    <figure className="ocr-map">
      <div className="ocr-map__frame">
        {src ? (
          <img className="ocr-map__page" src={src} alt="The page these readings came from" />
        ) : (
          <div className="ocr-map__placeholder">
            {failed ? 'The page image could not be loaded.' : 'Loading the page…'}
          </div>
        )}

        {/* The overlay is absolutely positioned over the image and sized in PERCENTAGES, so
            it tracks the image at every width without any measurement in JavaScript. */}
        <div className="ocr-map__overlay">
          {regions.map((region) => {
            const active = region.regionId === selectedRegionId;
            return (
              <button
                key={region.regionId}
                ref={active ? selectedRef : undefined}
                type="button"
                className={`ocr-box ocr-box--${region.confidenceBand}${active ? ' is-active' : ''}`}
                style={{
                  left: `${region.bbox.x * 100}%`,
                  top: `${region.bbox.y * 100}%`,
                  width: `${region.bbox.width * 100}%`,
                  height: `${region.bbox.height * 100}%`,
                }}
                aria-pressed={active}
                onClick={() => onSelect?.(active ? null : region)}
              >
                <span className="sr-only">
                  {region.text
                    ? `Region ${region.regionId + 1}: ${region.text}`
                    : `Region ${region.regionId + 1}: nothing could be read here`}
                </span>
                <span className="ocr-box__tag" aria-hidden="true">
                  {region.regionId + 1}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <figcaption className="ocr-map__legend">
        <span className="ocr-key ocr-key--read">{counts.read} read</span>
        <span className="ocr-key ocr-key--verify">{counts.verify} need checking</span>
        {counts.unreadable > 0 && (
          <span className="ocr-key ocr-key--unreadable">{counts.unreadable} unreadable</span>
        )}
      </figcaption>
    </figure>
  );
}

/** What one selected region says about itself. Rendered beside the page, not over it — a
 *  popover on top of the evidence hides the evidence. */
export function RegionDetail({ region }: { region: OcrRegion }): JSX.Element {
  return (
    <div className={`ocr-detail ocr-detail--${region.confidenceBand}`}>
      <div className="ocr-detail__head">
        Region {region.regionId + 1} · page {region.page}
      </div>

      <p className="ocr-detail__text">
        {region.text ? `“${region.text}”` : 'Nothing could be read from this region.'}
      </p>

      <dl className="ocr-detail__meta">
        <div>
          <dt>Confidence</dt>
          {/* NULL IS NOT ZERO. An engine that exposes no score has not said the reading is
              bad; it has said nothing, and printing 0% would be inventing a measurement. */}
          <dd>
            {region.confidence == null
              ? 'not measured'
              : `${Math.round(region.confidence * 100)}%`}
          </dd>
        </div>
        <div>
          <dt>Read by</dt>
          <dd>{region.backend}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{STATUS[region.confidenceBand]}</dd>
        </div>
        {region.cropWidth && region.cropHeight && (
          <div>
            <dt>Crop</dt>
            <dd>
              {region.cropWidth}×{region.cropHeight} px
            </dd>
          </div>
        )}
      </dl>

      {region.itemIds.length > 0 && (
        <p className="ocr-detail__links">
          Produced {region.itemIds.length}{' '}
          {region.itemIds.length === 1 ? 'entry' : 'entries'} below.
        </p>
      )}
    </div>
  );
}

const STATUS: Record<OcrRegion['confidenceBand'], string> = {
  high: 'read clearly',
  medium: 'read, worth a glance',
  verify: 'needs checking',
  unreadable: 'could not be read',
};
