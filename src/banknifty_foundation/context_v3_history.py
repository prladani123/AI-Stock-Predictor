"""Historical preparation preflight and orchestration for Context V3.

No provider is called unless the caller explicitly enables a network probe.
Absent evidence is reported as a blocker; it is never synthesized.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
from pathlib import Path

from .calendar import IST
from .config import ResearchContextSettings
from .context_v3 import (
    PROFILES,
    ContextV3CoverageService,
    ContextV3FeatureGenerationService,
    ContextV3PlanValidationService,
    ContextV3PlanningService,
    _hash,
    _stamp,
)
from .market_structure_context import ConstituentUniverseService, MarketStructureContextRepository
from .paths import memory_root
from .research_context import (
    DhanHistoricalOptionProvider,
    ResearchContextRepository,
    ResearchProviderError,
    UpstoxAnalyticsResearchProvider,
)

PREFLIGHT_VERSION = 'context-v3-history-preflight-v1'


def _provider_status(value, *, required=True):
    if not required:
        return 'NOT_REQUIRED'
    return {
        'READY_FOR_REQUEST': 'AVAILABLE',
        'TOKEN_EXPIRED': 'AUTH_UNSUPPORTED',
        'SUBSCRIPTION_REQUIRED': 'SUBSCRIPTION_REQUIRED',
        'NOT_CONFIGURED': 'NOT_CONFIGURED',
        'DISABLED': 'NOT_CONFIGURED',
    }.get(value, 'NOT_CONFIGURED')


class ContextV3HistoryPreflightService:
    def __init__(self, repository, root=None, context_repository=None):
        self.repository = repository
        self.root = Path(root or memory_root())
        self.contexts = context_repository or MarketStructureContextRepository()

    def inspect(self, revision_id, instrument_id, *, date_from, date_to,
                profiles=None, allow_network=False):
        start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
        if start > end:
            raise ValueError('INVALID_PREFLIGHT_DATE_RANGE')
        requested = list(PROFILES) if profiles in (None, 'all', ['all']) else list(profiles) if isinstance(profiles,(list,tuple)) else [
            part.strip() for part in str(profiles).split(',') if part.strip()]
        unknown = [name for name in requested if name not in PROFILES]
        if unknown:
            raise ValueError('UNKNOWN_CONTEXT_V3_PROFILE:' + ','.join(unknown))
        revision = self.repository.get_revision(str(revision_id))
        blockers = []
        if revision.status != 'ACCEPTED':
            blockers.append({'code': 'REVISION_NOT_ACCEPTED', 'status': revision.status})
        if int(instrument_id) not in revision.instrument_ids:
            blockers.append({'code': 'INSTRUMENT_NOT_IN_REVISION'})
        sessions = [item for item in self.repository.sessions_between(start, end)
                    if item.status in ('OPEN', 'SPECIAL')]
        dates = [item.session_date for item in sessions]
        if not dates:
            blockers.append({'code': 'NO_CANONICAL_SESSIONS'})
        baseline = self.repository.feature_rows(str(revision_id), int(instrument_id),
                                                'banknifty-bigmove-v2')
        labels = self.repository.big_move_labels(str(revision_id), int(instrument_id))
        baseline_days = Counter(row.as_of.astimezone(IST).date() for row in baseline
                                if row.quality_status == 'PASS')
        label_days = Counter(item.as_of.astimezone(IST).date() for item in labels
                             if item.quality_status == 'VALID')
        profile_reports = []
        for profile in requested:
            try:
                plan = ContextV3PlanningService(self.repository, self.root).plan(
                    revision_id, instrument_id, profile, date_from=start, date_to=end)
            except Exception as exc:
                plan={'status':'BLOCKED','profile':{'requiredBlocks':list(PROFILES[profile])},'pins':{},
                      'blockers':[{'code':'PROFILE_PLANNING_UNAVAILABLE','reason':str(exc)}]}
            relevant = [item for item in plan.get('blockers', [])
                        if item.get('date') is None or str(item.get('date')) in {str(day) for day in dates}]
            if relevant:
                blockers.extend([{**item, 'profileVersion': profile} for item in relevant])
            profile_reports.append({
                'profileVersion': profile,
                'planStatus': plan['status'],
                'planBlockers': relevant,
                'requiredBlocks': plan.get('profile', {}).get('requiredBlocks', []),
                'status':plan['status'], 'canonicalDates':[str(day) for day in dates],
                'v2Rows':sum(baseline_days[day] for day in dates),
                'labels':sum(label_days[day] for day in dates),
                'v3Rows':len([row for row in self.repository.feature_rows(str(revision_id),int(instrument_id),profile)
                              if start<=row.as_of.astimezone(IST).date()<=end]),
                'researchCompleteDates':sorted(plan.get('pins',{}).get('researchContextRunIds',{})),
                'structurePassDates':sorted(plan.get('pins',{}).get('marketStructureContextRunIds',{})),
                'officialMembershipDates':sorted(plan.get('pins',{}).get('membershipByDate',{})),
                'scheduledConfigStatus':'BLOCKED' if any('SCHEDULED' in r['code'] or r.get('file')=='scheduled_events.json' for r in relevant)
                    else 'AVAILABLE' if plan.get('pins',{}).get('scheduledConfigurationConfigured') else 'NOT_CONFIGURED',
                'ambiguousDates':sorted({r.get('date') for r in relevant if 'AMBIGUOUS' in r['code'] and r.get('date')}),
                'blockers':relevant,'nextActions':_next_actions(relevant),
                'pinnedDates': sorted(set(plan.get('pins', {}).get('researchContextRunIds', {})) |
                                      set(plan.get('pins', {}).get('marketStructureContextRunIds', {}))),
            })
        membership = ConstituentUniverseService(self.root)
        membership_days = {}
        for day in dates:
            try:
                snapshot = membership.resolve(day)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                membership_days[str(day)] = {'status': 'AMBIGUOUS_OFFICIAL_EVIDENCE' if 'Overlapping' in str(exc) or 'AMBIGUOUS' in str(exc) else 'INVALID', 'reason': str(exc)}
                continue
            membership_days[str(day)] = {
                'status': ('OFFICIAL_MEMBERSHIP_WITH_OFFICIAL_WEIGHTS' if all(m.weight is not None and m.weight_source_quality=='OFFICIAL_INDEX' for m in snapshot.members)
                           else 'OFFICIAL_MEMBERSHIP_EQUAL_WEIGHT_PROXY') if snapshot and snapshot.source_quality=='OFFICIAL_INDEX'
                           else 'TRACKING_PROXY_ONLY' if snapshot else 'LOCAL_DATA_MISSING',
                'snapshotId': snapshot.snapshot_id if snapshot else None,
                'sourceQuality': snapshot.source_quality if snapshot else None,
                'membershipAvailability':'OFFICIAL_MEMBERSHIP_AVAILABLE' if snapshot and snapshot.source_quality=='OFFICIAL_INDEX' else 'TRACKING_PROXY_ONLY' if snapshot else 'LOCAL_DATA_MISSING',
            }
        settings = ResearchContextSettings.from_env()
        dhan = DhanHistoricalOptionProvider(settings=settings, root=self.root)
        upstox = UpstoxAnalyticsResearchProvider(settings=settings, root=self.root)
        research_required = any(
            set(PROFILES[name]) & {'INDIA_MARKET', 'GLOBAL', 'DERIVATIVES', 'BANK_STRUCTURE'} for name in requested)
        derivatives_required = any(
            set(PROFILES[name]) & {'DERIVATIVES', 'BANK_STRUCTURE'} for name in requested)
        options_required = any('DERIVATIVES' in PROFILES[name] for name in requested)
        membership_required = any('BANK_STRUCTURE' in PROFILES[name] for name in requested)
        providers = {
            'DHAN_ROLLING_OPTIONS': {
                'status': _provider_status(dhan.configuration_state(),
                                           required=options_required),
                'capability': 'ROLLING_MONTH_ATM_OPTIONS',
            },
            'UPSTOX_HISTORICAL': {
                'status': _provider_status(upstox.configuration_state(), required=research_required),
                'capability': 'HISTORICAL_CANDLES_MARKET_INFORMATION',
            },
            'UPSTOX_PLUS': {
                'status': _provider_status(upstox.plus_state(), required=derivatives_required),
                'capability': 'EXPIRED_FUTURES_OPTIONS',
            },
            'LOCAL_OFFICIAL_MEMBERSHIP': {
                'status': 'AVAILABLE' if membership_required and membership_days and all(
                    row['status'].startswith('OFFICIAL_MEMBERSHIP') for row in membership_days.values())
                    else 'LOCAL_DATA_MISSING' if membership_required else 'NOT_REQUIRED',
                'capability': 'OFFICIAL_INDEX_MEMBERSHIP',
            },
        }
        for name, row in providers.items():
            if row['status'] in ('AUTH_UNSUPPORTED', 'SUBSCRIPTION_REQUIRED', 'NOT_CONFIGURED'):
                blockers.append({'code': 'PROVIDER_' + name + '_' + row['status']})
            elif row['status'] == 'LOCAL_DATA_MISSING':
                blockers.append({'code': 'PROVIDER_' + name + '_LOCAL_DATA_MISSING'})
        missing_v2 = [day for day in dates if not baseline_days[day]]
        missing_labels = [day for day in dates if not label_days[day]]
        if missing_v2:
            blockers.append({'code': 'MISSING_V2_FEATURES', 'dates': [str(day) for day in missing_v2]})
        if missing_labels:
            blockers.append({'code': 'MISSING_LABELS', 'dates': [str(day) for day in missing_labels]})
        for report in profile_reports:
            blocks=set(PROFILES[report['profileVersion']])
            required=[]
            if blocks & {'INDIA_MARKET','GLOBAL','DERIVATIVES','BANK_STRUCTURE'}: required.append('UPSTOX_HISTORICAL')
            if 'DERIVATIVES' in blocks: required.append('DHAN_ROLLING_OPTIONS')
            if blocks & {'DERIVATIVES','BANK_STRUCTURE'}: required.append('UPSTOX_PLUS')
            if 'BANK_STRUCTURE' in blocks: required.append('LOCAL_OFFICIAL_MEMBERSHIP')
            report['providerRequirements']={name:providers[name] for name in required}
        probes = {}
        if allow_network:
            probes = self._probes(dates[0] if dates else None, settings, self.root)
            for name, row in probes.items():
                if row['status'] in ('AUTH_UNSUPPORTED', 'SUBSCRIPTION_REQUIRED', 'PROVIDER_ERROR'):
                    blockers.append({'code': 'PROVIDER_PROBE_' + name + '_' + row['status']})
        result = {
            'kind': 'CONTEXT_V3_HISTORY_PREFLIGHT',
            'preflightVersion': PREFLIGHT_VERSION,
            'status': 'BLOCKED' if blockers else 'READY',
            'networkProbe': 'RUN' if allow_network else 'NOT_RUN',
            'revisionId': str(revision_id),
            'revisionChecksum': revision.checksum,
            'instrumentId': int(instrument_id),
            'dateFrom': str(start),
            'dateTo': str(end),
            'sessions': [str(day) for day in dates],
            'profiles': profile_reports,
            'coverage': {
                'v2FeatureDays': len([day for day in dates if baseline_days[day]]),
                'labelDays': len([day for day in dates if label_days[day]]),
                'missingV2FeatureDates': [str(day) for day in dates if not baseline_days[day]],
                'missingLabelDates': [str(day) for day in dates if not label_days[day]],
            },
            'membership': membership_days,
            'providers': providers,
            'providerProbes': probes,
            'blockers': blockers,
            'nextRequiredAction': _next_actions(blockers),
        }
        result['checksum'] = _hash(result)
        return result

    @staticmethod
    def _probes(day, settings, root):
        if day is None:
            return {}
        result = {}
        dhan = DhanHistoricalOptionProvider(settings=settings, root=root)
        try:
            observations = dhan.option_history(day)
            result['DHAN_ROLLING_OPTIONS'] = {
                'status': 'AVAILABLE' if observations else 'LOCAL_DATA_MISSING',
                'observationCount': len(observations),
            }
        except ResearchProviderError as exc:
            result['DHAN_ROLLING_OPTIONS'] = {'status': exc.state, 'reason': exc.reason}
        except (ValueError, TypeError, OSError):
            result['DHAN_ROLLING_OPTIONS'] = {'status': 'PROVIDER_ERROR', 'reason': 'PROBE_FAILED'}
        upstox = UpstoxAnalyticsResearchProvider(settings=settings, root=root)
        try:
            observations = upstox.historical_candles('BANKNIFTY', day)
            result['UPSTOX_HISTORICAL'] = {
                'status': 'AVAILABLE' if observations else 'LOCAL_DATA_MISSING',
                'observationCount': len(observations),
            }
        except ResearchProviderError as exc:
            result['UPSTOX_HISTORICAL'] = {'status': exc.state, 'reason': exc.reason}
        except (ValueError, TypeError, OSError):
            result['UPSTOX_HISTORICAL'] = {'status': 'PROVIDER_ERROR', 'reason': 'PROBE_FAILED'}
        return result


def _next_actions(blockers):
    codes = {item['code'] for item in blockers}
    actions = []
    if 'REVISION_NOT_ACCEPTED' in codes:
        actions.append('ACCEPT_AN_IMMUTABLE_DATASET_REVISION')
    if any(code.startswith('RESEARCH_CONTEXT') for code in codes):
        actions.append('IMPORT_OR_BACKFILL_PINNED_RESEARCH_CONTEXT')
    if any(code.startswith('MARKET_STRUCTURE') for code in codes):
        actions.append('ENRICH_PINNED_MARKET_STRUCTURE_CONTEXT')
    if any('MEMBERSHIP' in code for code in codes):
        actions.append('IMPORT_OFFICIAL_BANKNIFTY_MEMBERSHIP_CSV')
    if any(code.startswith('PROVIDER_') and 'LOCAL_OFFICIAL_MEMBERSHIP' not in code
           for code in codes):
        actions.append('CONFIGURE_OR_ENABLE_REQUIRED_PROVIDER')
    if any(code in ('NO_CANONICAL_SESSIONS', 'MISSING_LABELS', 'MISSING_V2_FEATURES') for code in codes):
        actions.append('GENERATE_ACCEPTED_V2_FEATURES_AND_LABELS')
    return actions


class ContextV3HistoryPreparationService:
    def __init__(self, repository, root=None):
        self.repository = repository
        self.root = Path(root or memory_root())

    def prepare(self, revision_id, instrument_id, *, date_from, date_to, profiles='all',
                output_dir, dry_run=False, allow_network=False):
        requested = list(PROFILES) if profiles in (None, 'all', ['all']) else list(profiles) if isinstance(profiles,(list,tuple)) else [
            part.strip() for part in str(profiles).split(',') if part.strip()]
        preflight = ContextV3HistoryPreflightService(self.repository, self.root).inspect(
            revision_id, instrument_id, date_from=date_from, date_to=date_to,
            profiles=requested, allow_network=False)
        preparation=[]
        if allow_network and not dry_run:
            from .research_context import ResearchContextEnrichmentService
            from .market_structure_context import MarketStructureContextService
            research_dates=set(); structure_dates=set()
            for report in preflight['profiles']:
                blocks=set(report['requiredBlocks'])
                if blocks & {'INDIA_MARKET','GLOBAL','DERIVATIVES'}:
                    research_dates.update(set(report['canonicalDates'])-set(report['researchCompleteDates'])-set(report['ambiguousDates']))
                if blocks & {'DERIVATIVES','BANK_STRUCTURE'}:
                    structure_dates.update(set(report['canonicalDates'])-set(report['structurePassDates'])-set(report['ambiguousDates']))
            for day in sorted(research_dates):
                try:
                    required=set().union(*(set(report['requiredBlocks']) for report in preflight['profiles']
                        if day in report['canonicalDates'] and day not in report['researchCompleteDates']))
                    result=ResearchContextEnrichmentService(root=self.root).enrich(day,day,missing_only=True,required_blocks=required)
                    preparation.append({'date':day,'kind':'RESEARCH_CONTEXT','result':result})
                except Exception as exc:
                    preparation.append({'date':day,'kind':'RESEARCH_CONTEXT','status':'UNAVAILABLE','reason':str(exc)})
            for day in sorted(structure_dates):
                membership=preflight['membership'].get(day,{})
                if not membership.get('status','').startswith('OFFICIAL_MEMBERSHIP'):
                    preparation.append({'date':day,'kind':'MARKET_STRUCTURE','status':'LOCAL_DATA_MISSING'})
                    continue
                try:
                    result=MarketStructureContextService(root=self.root).enrich_day(date.fromisoformat(day),missing_only=True)
                    preparation.append({'date':day,'kind':'MARKET_STRUCTURE','result':result})
                except Exception as exc:
                    preparation.append({'date':day,'kind':'MARKET_STRUCTURE','status':'UNAVAILABLE','reason':str(exc)})
            preflight=ContextV3HistoryPreflightService(self.repository,self.root).inspect(
                revision_id,instrument_id,date_from=date_from,date_to=date_to,profiles=requested,allow_network=False)
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        preflight_path = output / 'preflight.json'
        preflight_path.write_text(json.dumps(preflight, indent=2, sort_keys=True, default=str),
                                  encoding='utf-8')
        planner = ContextV3PlanningService(self.repository, self.root)
        validator = ContextV3PlanValidationService(self.repository, self.root)
        generator = ContextV3FeatureGenerationService(self.repository, root=self.root)
        outcomes = []
        for profile in requested:
            outcome = {'profileVersion': profile, 'status': 'BLOCKED'}
            try:
                plan = planner.plan(revision_id, instrument_id, profile,
                                    date_from=date_from, date_to=date_to)
            except Exception as exc:
                outcome['reason']=str(exc); outcomes.append(outcome); continue
            plan_path = output / f'{profile}.plan.json'
            plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True, default=str),
                                 encoding='utf-8')
            outcome.update(planPath=str(plan_path), planStatus=plan['status'])
            if plan['status'] != 'READY':
                outcome['planBlockers'] = plan.get('blockers', [])
                outcomes.append(outcome)
                continue
            if dry_run:
                outcome['status'] = 'READY'
                outcomes.append(outcome)
                continue
            try:
                pins = validator.validate(plan, revision_id, instrument_id, profile)
                rows, inserted = generator.generate(
                    revision_id, instrument_id, profile,
                    research_context_runs=pins['researchContextRuns'],
                    market_structure_runs=pins['marketStructureRuns'],
                    membership_snapshot_checksums=pins['membershipSnapshotChecksums'],
                    scheduled_configuration=pins['scheduledConfiguration'],
                    market_structure_rules=pins['marketStructureRules'],
                    date_from=date_from, date_to=date_to)
            except Exception as exc:
                outcome.update(status='BLOCKED',reason=str(exc))
                outcomes.append(outcome)
                continue
            unavailable = [row for row in rows if row.quality_status != 'PASS']
            outcome.update(
                status='PASS' if rows and not unavailable else 'PARTIAL' if rows else 'UNAVAILABLE',
                rows={'considered': len(rows), 'generated': inserted, 'reused': len(rows) - inserted},
                passCount=len(rows) - len(unavailable), unavailableCount=len(unavailable))
            outcomes.append(outcome)
        try:
            coverage = ContextV3CoverageService().coverage()
        except Exception as exc:
            coverage = {'status': 'UNAVAILABLE', 'error': str(exc)}
        statuses = [row['status'] for row in outcomes]
        overall = 'READY' if dry_run and statuses and all(value == 'READY' for value in statuses) else \
            'PASS' if statuses and all(value == 'PASS' for value in statuses) else \
            'PARTIAL' if any(value in ('PASS', 'PARTIAL', 'READY') for value in statuses) else 'BLOCKED'
        return {'status': overall, 'dryRun': bool(dry_run), 'preflight': preflight,
                'preflightPath': str(preflight_path), 'profiles': outcomes, 'coverage': coverage,'preparation':preparation}


class ContextV3ResearchRunService:
    def __init__(self,repository,root=None):
        self.repository=repository; self.root=root

    def run(self,revision_id,instrument_id,*,date_from,date_to,profiles='all',
            algorithms=('LOGISTIC_REGRESSION','LIGHTGBM'),horizons=(10,20,30),
            output_dir,dry_run=False,allow_network=False):
        from .context_v3_experiments import ContextV3ExperimentService
        prepared=ContextV3HistoryPreparationService(self.repository,self.root).prepare(
            revision_id,instrument_id,date_from=date_from,date_to=date_to,profiles=profiles,
            output_dir=output_dir,dry_run=dry_run,allow_network=allow_network and not dry_run)
        experiments=[]
        for profile in prepared['profiles']:
            result={'profileVersion':profile['profileVersion'],'requestedAlgorithms':list(algorithms),'requestedHorizons':list(horizons)}
            if dry_run: result.update(status='NOT_RUN_DRY_RUN')
            elif profile['status'] not in ('PASS','PARTIAL'): result.update(status='BLOCKED_BY_DATA')
            else:
                try:
                    experiment=ContextV3ExperimentService(self.repository).run(
                        revision_id,instrument_id,profile['profileVersion'],date_from=date_from,date_to=date_to,
                        algorithms=algorithms,horizons=horizons)
                    result.update(status=experiment['status'],experimentId=experiment['manifest']['experimentId'],
                                  experimentChecksum=experiment['manifest']['checksum'])
                except Exception as exc: result.update(status='BLOCKED',reason=str(exc))
            experiments.append(result)
        # Execution counters and wall-clock times are deliberately not artifact identity.
        summary={'schemaVersion':'context-v3-research-run-v1','revisionId':str(revision_id),'instrumentId':instrument_id,
            'dateFrom':str(date_from),'dateTo':str(date_to),'dryRun':bool(dry_run),
            'preflightChecksum':prepared['preflight']['checksum'],'experiments':experiments}
        summary['checksum']=_hash(summary)
        target=Path(output_dir)/('research-run-'+summary['checksum']+'.json')
        target.write_text(json.dumps(summary,sort_keys=True,indent=2),encoding='utf-8')
        return {'summary':summary,'summaryPath':str(target),'preparation':prepared}
