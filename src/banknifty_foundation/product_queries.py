from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os
from typing import Any, Iterator


HORIZONS = (5, 10, 15, 20)


class ProductDataReader:
    """Read-only product views over the Python-owned PostgreSQL research tables."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if not self.database_url:
            raise RuntimeError("DATABASE_NOT_CONFIGURED")
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                yield connection

    @staticmethod
    def _prediction(row: dict[str, Any]) -> dict[str, Any]:
        calibrated = row["calibrated_probabilities"]
        probabilities = calibrated or row["raw_probabilities"] or {}
        confidence = max((float(value) for value in probabilities.values()), default=None)
        outcome = row["actual_class"]
        return {
            "id": row["prediction_id"],
            "available": True,
            "horizonMinutes": row["horizon_minutes"],
            "rawClass": row["raw_class"],
            "direction": row["final_class"],
            "rawProbabilities": row["raw_probabilities"],
            "calibratedProbabilities": calibrated,
            "confidence": confidence,
            "systemAbstained": row["system_abstained"],
            "abstentionReasons": row["abstention_reasons"] or [],
            "abstentionReason": ", ".join(row["abstention_reasons"] or []) or None,
            "algorithm": row["algorithm"],
            "model": row["model_version"],
            "modelVersion": row["model_version"],
            "modelChecksum": row["model_checksum"],
            "calibrationMethod": row["calibration_method"],
            "calibrationVersion": row["calibration_version"],
            "reliabilityBucket": row["reliability_bucket"],
            "asOf": row["as_of"].isoformat(),
            "createdAt": row["created_replay_clock"].isoformat(),
            "expiresAt": row["expires_at"].isoformat(),
            "actualOutcome": outcome,
            "qualityStatus": row["quality_status"],
            "resolved": outcome is not None,
        }

    @staticmethod
    def _prediction_select() -> str:
        return """
            SELECT p.prediction_id, p.horizon_minutes, p.as_of, p.expires_at,
                   p.raw_probabilities, p.calibrated_probabilities, p.raw_class,
                   p.calibrated_class, p.final_class, p.system_abstained,
                   p.abstention_reasons, p.reliability_bucket,
                   p.created_replay_clock, p.model_checksum,
                   model.algorithm, model.model_version,
                   calibration.method AS calibration_method,
                   calibration.version AS calibration_version,
                   outcome.actual_class, outcome.quality_status
            FROM ml_predictions p
            JOIN model_definitions model ON model.model_checksum = p.model_checksum
            LEFT JOIN calibration_runs calibration
              ON calibration.calibration_checksum = p.calibration_checksum
            LEFT JOIN ml_prediction_outcomes outcome
              ON outcome.prediction_id = p.prediction_id
        """

    def latest_predictions(self) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "reason": "DATABASE_NOT_CONFIGURED", "predictions": []}
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT DISTINCT ON (horizon_minutes) * FROM (" + self._prediction_select() +
                    ") product_predictions ORDER BY horizon_minutes, as_of DESC"
                ).fetchall()
            predictions = [self._prediction(row) for row in rows]
            predictions.sort(key=lambda item: item["horizonMinutes"])
            return {"available": bool(predictions), "reason": None if predictions else "NO_PREDICTIONS", "predictions": predictions}
        except Exception:
            return {"available": False, "reason": "DATABASE_UNAVAILABLE", "predictions": []}

    def prediction_history(
        self, *, page: int, page_size: int, date_value: str | None = None, horizon: int | None = None,
        direction: str | None = None, resolved: bool | None = None,
    ) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "reason": "DATABASE_NOT_CONFIGURED", "items": [], "page": page, "pageSize": page_size, "total": 0}
        clauses: list[str] = []
        values: list[Any] = []
        if date_value:
            clauses.append("(as_of AT TIME ZONE 'Asia/Kolkata')::date = %s"); values.append(date_value)
        if horizon is not None:
            clauses.append("horizon_minutes = %s"); values.append(horizon)
        if direction is not None:
            clauses.append("final_class = %s"); values.append(direction)
        if resolved is not None:
            clauses.append("actual_class IS NOT NULL" if resolved else "actual_class IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        try:
            with self._connection() as connection:
                base = "SELECT * FROM (" + self._prediction_select() + ") product_predictions"
                total = connection.execute("SELECT count(*) AS total FROM (" + base + where + ") counted", values).fetchone()["total"]
                rows = connection.execute(base + where + " ORDER BY as_of DESC, horizon_minutes LIMIT %s OFFSET %s", [*values, page_size, (page - 1) * page_size]).fetchall()
            return {"available": bool(rows), "reason": None if rows else "NO_PREDICTIONS", "items": [self._prediction(row) for row in rows], "page": page, "pageSize": page_size, "total": total}
        except Exception:
            return {"available": False, "reason": "DATABASE_UNAVAILABLE", "items": [], "page": page, "pageSize": page_size, "total": 0}

    def model_status(self) -> dict[str, Any]:
        empty=[{'horizonMinutes':h,'status':'NO_MODEL','reason':'NOT_TRAINED','approvalResult':'NOT_EVALUATED',
                'approvalReasons':[],'metrics':None} for h in (10,20,30)]
        base={'available':False,'bundleStatus':'NO_MODEL','models':empty,'legacyModels':[], 'comparisons':[],
              'disclaimer':'Chronological research evidence is not a guarantee of trading performance.'}
        if not self.database_url:
            return {**base,'reason':'DATABASE_NOT_CONFIGURED'}
        try:
            with self._connection() as connection:
                rows=connection.execute('''SELECT DISTINCT ON(m.horizon_minutes) m.*,
                    e.metrics AS evaluation_metrics,e.evaluation_checksum,
                    a.decision,a.reasons,a.approval_checksum,a.policy_version,
                    c.selected_algorithm,c.candidate_results,c.selection_reason,
                    EXISTS (SELECT 1 FROM big_move_model_definitions d WHERE d.stage='DIRECTION'
                        AND d.model_version=m.model_version AND d.artifact_checksum=m.artifact_checksum
                        AND d.dataset_revision_id=m.dataset_revision_id AND d.horizon_minutes=m.horizon_minutes
                        AND d.feature_set_version=m.feature_set_version AND d.status=m.status) AS paired
                    FROM big_move_model_definitions m
                    LEFT JOIN LATERAL (SELECT * FROM big_move_evaluation_runs e
                        WHERE e.candidate_model_checksum=COALESCE(m.manifest->>'approved_from_model_checksum',m.model_checksum)
                          AND (m.status<>'APPROVED' OR e.evaluation_checksum=m.manifest->>'evaluation_checksum')
                        ORDER BY e.created_at DESC,e.evaluation_checksum DESC LIMIT 1) e ON true
                    LEFT JOIN LATERAL (SELECT * FROM big_move_approval_decisions a
                        WHERE a.candidate_model_checksum=COALESCE(m.manifest->>'approved_from_model_checksum',m.model_checksum)
                          AND a.evaluation_checksum=e.evaluation_checksum
                          AND (m.status<>'APPROVED' OR a.approval_checksum=m.manifest->>'approval_decision_checksum')
                        ORDER BY a.created_at DESC,a.approval_checksum DESC LIMIT 1) a ON true
                    LEFT JOIN LATERAL (SELECT * FROM big_move_model_comparisons c
                        WHERE c.dataset_revision_id=m.dataset_revision_id AND c.horizon_minutes=m.horizon_minutes
                          AND c.instrument_id=(m.manifest->>'instrument_id')::bigint
                        ORDER BY c.created_at DESC,c.comparison_checksum DESC LIMIT 1) c ON true
                    WHERE m.stage='MOVE_DETECTOR'
                    ORDER BY m.horizon_minutes,(m.status='APPROVED' AND a.decision='PASS') DESC NULLS LAST,
                             m.created_at DESC,m.model_version DESC''').fetchall()
            models={m['horizonMinutes']:m for m in empty}
            comparisons=[]
            for row in rows:
                manifest=row['manifest']; approved=row['status']=='APPROVED' and row['decision']=='PASS' and row['paired']
                reason=(None if approved else 'APPROVAL_FAILED' if row['decision']=='FAIL' else
                    'INCOMPLETE_MODEL_BUNDLE' if not row['paired'] else
                    'INSUFFICIENT_EVIDENCE' if row['decision']=='INSUFFICIENT_EVIDENCE' or
                        (row['candidate_results'] and not row['selected_algorithm']) else
                    'NOT_EVALUATED' if row['evaluation_checksum'] is None else 'AWAITING_APPROVAL')
                status='APPROVED' if approved else 'FAILED_EVIDENCE' if row['decision']=='FAIL' else 'CANDIDATE'
                models[row['horizon_minutes']]={'horizonMinutes':row['horizon_minutes'],'status':status,'reason':reason,
                    'algorithm':row['algorithm'],'version':row['model_version'],
                    'trainingRevision':str(row['dataset_revision_id']),'featureCount':len(manifest.get('feature_schema',[])),
                    'trainingRows':manifest.get('rows_used'),'calibration':manifest.get('calibration'),
                    'decisionThreshold':manifest.get('moveDecisionThreshold'),'metrics':row['evaluation_metrics'],
                    'approvalResult':row['decision'] or ('INSUFFICIENT_EVIDENCE' if reason=='INSUFFICIENT_EVIDENCE' else 'NOT_EVALUATED'),
                    'approvalReasons':row['reasons'] or ([reason] if reason else []),
                    'artifactChecksum':row['artifact_checksum'],'modelChecksum':row['model_checksum'],
                    'evaluationChecksum':row['evaluation_checksum'],'approvalChecksum':row['approval_checksum'],
                    'policyVersion':row['policy_version']}
                if row['candidate_results']:
                    comparisons.append({'horizonMinutes':row['horizon_minutes'],'selectedAlgorithm':row['selected_algorithm'],
                        'candidates':row['candidate_results'],'selectionReason':row['selection_reason']})
            values=list(models.values())
            return {**base,'available':bool(rows),'reason':None if rows else 'NOT_TRAINED',
                'bundleStatus':'APPROVED' if all(m['status']=='APPROVED' for m in values) else
                    'PARTIALLY_APPROVED' if any(m['status']=='APPROVED' for m in values) else 'CANDIDATE' if rows else 'NO_MODEL',
                'models':values,'comparisons':comparisons,'legacyModels':self._legacy_model_status().get('models',[])}
        except Exception:
            return {**base,'bundleStatus':'UNAVAILABLE','reason':'DATABASE_UNAVAILABLE',
                    'models':[{**m,'reason':'DATABASE_UNAVAILABLE'} for m in empty]}

    def _legacy_model_status(self) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "reason": "DATABASE_NOT_CONFIGURED", "bundleStatus": "UNAVAILABLE", "models": []}
        try:
            with self._connection() as connection:
                rows = connection.execute("""
                    SELECT DISTINCT ON (model.horizon_minutes)
                           model.horizon_minutes, model.algorithm, model.model_version,
                           model.artifact_checksum, model.created_at, training.manifest,
                           calibration.method, calibration.version AS calibration_version,
                           calibration.metrics
                    FROM model_definitions model
                    LEFT JOIN training_runs training ON training.selected_model_checksum = model.model_checksum
                    LEFT JOIN calibration_runs calibration ON calibration.model_checksum = model.model_checksum
                    ORDER BY model.horizon_minutes, model.created_at DESC
                """).fetchall()
            models = []
            for row in rows:
                partitions = (row["manifest"] or {}).get("partitions", {})
                dates = partitions.get("train_dates", [])
                models.append({"horizonMinutes": row["horizon_minutes"], "algorithm": row["algorithm"],
                    "version": row["model_version"], "status": "CANDIDATE",
                    "trainingRange": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
                    "calibration": row["method"] or "UNAVAILABLE", "calibrationVersion": row["calibration_version"],
                    "metrics": row["metrics"], "artifactValid": bool(row["artifact_checksum"])})
            return {"available": bool(models), "reason": None if models else "NO_MODELS", "bundleStatus": "CANDIDATE", "disclaimer": "Research metrics are not guaranteed trading performance.", "models": models}
        except Exception:
            return {"available": False, "reason": "DATABASE_UNAVAILABLE", "bundleStatus": "UNAVAILABLE", "models": []}

    @staticmethod
    def _intelligence_horizon(row: dict[str, Any]) -> dict[str, Any]:
        probability = float(row["big_move_probability"])
        quality = row["quality_status"]
        action = "DATA_NOT_READY" if quality != "PASS" else (
            "STRONG_SETUP" if probability >= 0.70 and row["direction"] else
            "WATCH" if probability >= 0.55 else "WAIT"
        )
        return {
            "id": row["prediction_id"], "available": True,
            "minutes": row["horizon_minutes"],
            "bigMoveProbability": probability,
            "quietProbability": float(row["quiet_probability"]),
            "state": row["state"], "direction": row["direction"],
            "directionProbability": float(row["direction_probability"]) if row["direction_probability"] is not None else None,
            "upGivenBigMoveProbability": float(row["up_given_big_move_probability"]) if row["up_given_big_move_probability"] is not None else None,
            "downGivenBigMoveProbability": float(row["down_given_big_move_probability"]) if row["down_given_big_move_probability"] is not None else None,
            "expectedMovePoints": float(row["expected_move_points"]) if row["expected_move_points"] is not None else None,
            "expectedMovePercent": float(row["expected_move_percent"]) if row["expected_move_percent"] is not None else None,
            "modelVersion": row["model_version"], "qualityStatus": quality,
            "marketRegime": row.get("market_regime"),
            "actionState": action,
            "moveAlgorithm": row.get("move_algorithm"),
            "directionAlgorithm": row.get("direction_algorithm"),
            "referencePrice": float(row["reference_price"]) if row.get("reference_price") is not None else None,
            "expiresAt": row["expires_at"].isoformat(), "asOf": row["as_of"].isoformat(),
            "actualResult": row.get("actual_label"),
            "resolved": row.get("actual_label") is not None,
        }

    def current_intelligence(self, *, as_of=None, instrument_id=None) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "reason": "DATABASE_NOT_CONFIGURED", "horizons": []}
        try:
            with self._connection() as connection:
                rows = connection.execute("""
                    SELECT DISTINCT ON (prediction.horizon_minutes)
                           prediction.prediction_id, prediction.horizon_minutes,
                           prediction.as_of, prediction.expires_at, prediction.reference_price,
                           big_move_probability, quiet_probability, state, direction,
                           direction_probability, up_given_big_move_probability,
                           down_given_big_move_probability, expected_move_points,
                           expected_move_percent, prediction.model_version, market_regime,
                           session_bias, prediction.quality_status,
                           move_model.algorithm AS move_algorithm,
                           direction_model.algorithm AS direction_algorithm,
                           outcome.actual_label
                    FROM big_move_predictions prediction
                    JOIN big_move_model_definitions move_model
                      ON move_model.model_checksum = prediction.move_model_checksum
                    JOIN big_move_model_definitions direction_model
                      ON direction_model.model_checksum = prediction.direction_model_checksum
                    LEFT JOIN big_move_prediction_outcomes outcome
                      ON outcome.prediction_id = prediction.prediction_id
                    WHERE move_model.status='APPROVED' AND direction_model.status='APPROVED'
                      AND (%s::timestamptz IS NULL OR prediction.as_of=%s)
                      AND (%s::bigint IS NULL OR prediction.instrument_id=%s)
                      AND (prediction.as_of AT TIME ZONE 'Asia/Kolkata')::date=(now() AT TIME ZONE 'Asia/Kolkata')::date
                      AND prediction.expires_at + interval '1 minute' > now()
                    ORDER BY prediction.horizon_minutes, prediction.as_of DESC
                """,(as_of,as_of,instrument_id,instrument_id)).fetchall()
                experimental = connection.execute("""
                    SELECT output_id, as_of, model_name, model_version, forecast_path, summary
                    FROM experimental_model_outputs ORDER BY as_of DESC LIMIT 1
                """).fetchone()
            rows = sorted(rows, key=lambda item: item["horizon_minutes"])
            latest = max(rows, key=lambda item: item["as_of"]) if rows else None
            return {"available": bool(rows), "reason": None if rows else "NO_BIG_MOVE_PREDICTIONS",
                "asOf": latest["as_of"].isoformat() if latest else None,
                "marketRegime": latest["market_regime"] if latest else "UNKNOWN",
                "sessionBias": latest["session_bias"] if latest else {"direction": "NEUTRAL", "confidence": 0.0, "drivers": []},
                "qualityStatus": latest["quality_status"] if latest else "UNAVAILABLE",
                "horizons": [self._intelligence_horizon(row) for row in rows],
                "featureValues": (latest['session_bias'].get('runtime_lineage') or {}).get('feature_values') if latest else None,
                "featureAsOf": latest['as_of'].isoformat() if latest else None,
                "featureQualityStatus": latest['quality_status'] if latest else None,
                "kronosExperimental": ({"available": True, "id": experimental["output_id"],
                    "asOf": experimental["as_of"].isoformat(), "model": experimental["model_name"],
                    "modelVersion": experimental["model_version"], "forecastPath": experimental["forecast_path"],
                    "summary": experimental["summary"]}
                    if experimental else {"available": False, "reason": "NO_EXPERIMENTAL_OUTPUT"})}
        except Exception:
            return {"available": False, "reason": "DATABASE_UNAVAILABLE", "horizons": []}

    def intelligence_history(
        self, *, page: int, page_size: int, date_value: str | None = None,
        horizon: int | None = None, direction: str | None = None, resolved: bool | None = None,
    ) -> dict[str, Any]:
        if not self.database_url:
            return {"available": False, "reason": "DATABASE_NOT_CONFIGURED", "items": [], "page": page, "pageSize": page_size, "total": 0}
        try:
            clauses: list[str] = []
            values: list[Any] = []
            if date_value:
                clauses.append("(prediction.as_of AT TIME ZONE 'Asia/Kolkata')::date = %s"); values.append(date_value)
            if horizon is not None:
                clauses.append("prediction.horizon_minutes = %s"); values.append(horizon)
            if direction is not None:
                clauses.append("prediction.direction = %s"); values.append(direction)
            if resolved is not None:
                clauses.append("outcome.actual_label IS NOT NULL" if resolved else "outcome.actual_label IS NULL")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            base = """
                FROM big_move_predictions prediction
                JOIN big_move_model_definitions move_model
                  ON move_model.model_checksum = prediction.move_model_checksum
                JOIN big_move_model_definitions direction_model
                  ON direction_model.model_checksum = prediction.direction_model_checksum
                LEFT JOIN big_move_prediction_outcomes outcome
                  ON outcome.prediction_id = prediction.prediction_id
            """
            with self._connection() as connection:
                total = connection.execute("SELECT count(*) AS total " + base + where, values).fetchone()["total"]
                rows = connection.execute("""
                    SELECT prediction.prediction_id, prediction.horizon_minutes,
                           prediction.as_of, prediction.expires_at, prediction.reference_price,
                           prediction.big_move_probability, prediction.quiet_probability,
                           prediction.state, prediction.direction, prediction.direction_probability,
                           prediction.up_given_big_move_probability,
                           prediction.down_given_big_move_probability,
                           prediction.expected_move_points, prediction.expected_move_percent,
                           prediction.model_version, prediction.market_regime, prediction.quality_status,
                           move_model.algorithm AS move_algorithm,
                           direction_model.algorithm AS direction_algorithm,
                           outcome.actual_label
                """ + base + where + " ORDER BY prediction.as_of DESC, prediction.horizon_minutes LIMIT %s OFFSET %s",
                    [*values, page_size, (page - 1) * page_size]).fetchall()
            return {"available": bool(rows), "reason": None if rows else "NO_BIG_MOVE_PREDICTIONS",
                "items": [self._intelligence_horizon(row) for row in rows], "page": page,
                "pageSize": page_size, "total": total}
        except Exception:
            return {"available": False, "reason": "DATABASE_UNAVAILABLE", "items": [], "page": page, "pageSize": page_size, "total": 0}
