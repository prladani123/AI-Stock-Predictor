import type { Prediction } from '../types';
import { formatIst } from '../hooks';
export function PredictionCard({prediction, onSelect}: {prediction: Prediction; onSelect: (value: Prediction) => void}) {
  const direction = prediction.direction ?? 'UNAVAILABLE';
  return <button className={`prediction ${direction.toLowerCase()}`} onClick={() => onSelect(prediction)}>
    <span>{prediction.horizonMinutes} MIN</span><strong>{direction.replace('_', ' ')}</strong>
    <b>{prediction.confidence == null ? '-' : `${Math.round(prediction.confidence * 100)}%`}</b>
    <small>{prediction.available ? `${prediction.algorithm ?? 'Model'} / ${prediction.model ?? prediction.modelVersion ?? '-'}` : prediction.abstentionReason ?? 'No approved model'}</small>
    <time>{prediction.available ? `${prediction.resolved ? 'Resolved' : 'Pending'} | ${formatIst(prediction.asOf)} -> ${formatIst(prediction.expiresAt)}` : formatIst(prediction.asOf)}</time>
  </button>;
}
