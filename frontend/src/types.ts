export type Direction = 'UP' | 'DOWN' | 'NO_TRADE';
export interface EvidenceMetrics { samples:number; resolvedSamples:number; excludedOutcomes:number; predictedBigMoveCount:number; actualBigMoveCount:number; movePrecision:number|null; moveRecall:number|null; directionSamples:number; directionAccuracy:number|null; noBigMoveAccuracy:number|null; coverage:number|null; actualBigMoveRate:number|null; status:string; movePrecisionDifference:number|null; directionAccuracyDifference:number|null }
export type EvidenceAlignment = 'SUPPORTS_MODEL'|'CONTRADICTS_MODEL'|'NEUTRAL'|'UNAVAILABLE';
export type EvidenceCombined = 'MODEL_ONLY'|'MODEL_PLUS_MEMORY'|'MODEL_PLUS_NEWS'|'MODEL_PLUS_MEMORY_AND_NEWS'|'MODEL_CONTRADICTED_BY_MEMORY'|'MODEL_CONTRADICTED_BY_NEWS';
export interface EvidenceSummary { available:boolean; resolvedPredictions:number; minimumSample:number; notice:string; definitions:Record<string,string>; horizons:Array<{minutes:number; baseline:EvidenceMetrics; numericalMemory:Record<EvidenceAlignment,EvidenceMetrics>; newsMemory:Record<EvidenceAlignment,EvidenceMetrics>; confirmation:Record<string,EvidenceMetrics>; combined:Record<EvidenceCombined,EvidenceMetrics>; contradicted:EvidenceMetrics}> }
export interface EvidenceRecord { journal_id:string; prediction_id:string; instrument_id:number; market_as_of:string; decision_at:string; horizon_minutes:number; model_version:string; move_model_checksum:string; direction_model_checksum:string; evaluation_checksum:string|null; approval_checksum:string|null; approval_policy_version:string|null; baseline_prediction:{state:string; bigMoveProbability:number; direction:string|null; [key:string]:unknown}; market_evidence:Record<string,unknown>; numerical_memory:Record<string,unknown>; news_memory:Record<string,unknown>; evidence_alignment:{numericalMemory:string;newsMemory:string;marketConfirmation:string;confirmationState:string;[key:string]:unknown}; source_watermarks:Record<string,unknown>; journal_checksum:string; created_at:string; resolved:boolean; actual_label:string|null; outcome_quality:string|null; attached_at:string|null; realized_summary:Record<string,unknown>|null }
export interface EvidenceJournalResponse { available:boolean; reason:string|null; items:EvidenceRecord[]; total:number; page:number; pageSize:number }
export interface MemoryCoverage {dailyMemoryDays:number;minuteDataDays:number;completeMinuteDays:number;numericalEpisodes:number;structuredEvents:number;reactionMeasurableEvents:number;completeReactionEvents:number;missingMinuteDates:string[]}
export interface ScheduledEvent {eventType:string;title:string;scheduledDate:string;calendarDate:string;scheduledAt:string|null;timingPrecision:'EXACT_TIME'|'DATE_ONLY';source:string;sourceUrl:string;marketRelevance:string;bankniftyRelevance:string;country:string;expectedReleaseTimeKnown:boolean;sourceObservedAt:string;knowledgeAvailableAt:string;meetingStart:string|null;meetingEnd:string|null}
export interface ScheduledEventResponse {status:'CONFIGURED'|'NOT_CONFIGURED';configuredEventCount:number;configuredDateRange:string[];configuredEventTypes:string[];visibleEventCount:number;visibleEvents:ScheduledEvent[];events:ScheduledEvent[]}
export interface NewsMonitor {state:string;lastAttempt?:string|null;lastSuccessfulRefresh?:string|null;lastError?:string|null;consecutiveFailures:number}
export interface EventIntelligence {available:boolean;event?:NewsEvent;historicalMemory?:{matchedEvents:number;eventSimilarityStrength:string;reason?:string;historicalReaction:Record<string,{sampleSize:number;averageReturn:number|null;bigMoveRate:number|null;upRate:number|null;downRate:number|null}>;topMatches:Array<{eventId:string;date:string;category:string;summary:string;similarity:number}>};marketConfirmation?:{state:string;drivers:string[]}}
export interface NewsEvent {stance?:string;scope?:string;secondary_categories?:string[];timing_confidence?:string;source_metadata?:{sources?:Array<{name:string;url:string}>}}
export interface NewsStatusResponse {monitor?:NewsMonitor}
export type BigMoveDirection = 'UP' | 'DOWN';
export type ActionState = 'STRONG_SETUP' | 'WATCH' | 'WAIT' | 'DATA_NOT_READY';
export interface IntelligenceHorizon { id: string | null; available: boolean; minutes: 10 | 20 | 30; bigMoveProbability: number | null; quietProbability: number | null; state: 'BIG_MOVE' | 'NO_BIG_MOVE' | 'WAIT'; direction: BigMoveDirection | null; directionProbability: number | null; upGivenBigMoveProbability: number | null; downGivenBigMoveProbability: number | null; expectedMovePoints: number | null; expectedMovePercent: number | null; modelVersion: string | null; marketRegime?: string | null; qualityStatus: string; actionState: ActionState; moveAlgorithm: string | null; directionAlgorithm: string | null; referencePrice: number | null; asOf: string | null; expiresAt: string | null; actualResult: string | null; resolved: boolean }
export interface SessionBias { direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL' | 'MIXED'; confidence: number; drivers: string[] }
export interface ExternalResearchHorizon { minutes:number; pMeaningfulMove:number; pUpGivenMeaningfulMove:number; pDownGivenMeaningfulMove:number; direction:string; expectedReturn:number; expectedMagnitude:number; median:number; p05:number; p25:number; p50:number; p75:number; p95:number; maxUpExcursion:number; maxDownExcursion:number; uncertainty:number; dispersion:number; pathCount:number }
export interface ExperimentalForecast { available: boolean; reason?: string; id?: string; asOf?: string; referenceMinute?: string; computedAt?: string; availableAt?: string; status?: string; pointInTimeStatus?: string; model?: string; modelVersion?: string; horizons?: Record<string, ExternalResearchHorizon>; forecastPath?: Array<{timestamp: string; open: number; high: number; low: number; close: number}>; summary?: Record<string, unknown>; researchOnly?: boolean; usedByPriceCoreV2?: boolean }
export interface BankniftyContext { price: number | null; vwap: number | null; vwapRelationship: string; sessionHigh: number | null; sessionLow: number | null; openingHigh: number | null; openingLow: number | null }
export interface PriceEvidence { shortTrend: string; return5m: number | null; return10m: number | null; vwapRelationship: string; vwapDistancePercent: number | null; rangeExpansion: number | null; breakoutHighDistancePercent: number | null; breakoutLowDistancePercent: number | null }
export interface FuturesEvidence { available: boolean; price: number | null; premium: number | null; premiumPercent: number | null; oi: number | null; oiChange1m: number | null; oiChange5m: number | null; volume: number | null; buildup: string }
export interface OptionsEvidence { available: boolean; pcrOi: number | null; totalCeOi: number | null; totalPeOi: number | null; totalCeOiChange: number | null; totalPeOiChange: number | null; oiChangeImbalance: number | null; atmStraddle: number | null; strongCeOi: number | null; strongPeOi: number | null; completeness: number | null; ivStatus: string }
export interface VolatilityEvidence { realized5m: number | null; realized10m: number | null; realized20m: number | null; atrNormalized: number | null; vixStatus: string }
export interface ImportantLevels { current: number | null; vwap: number | null; atm: number | null; support: number | null; resistance: number | null; strongCeOi: number | null; strongPeOi: number | null; openingHigh: number | null; openingLow: number | null; sessionHigh: number | null; sessionLow: number | null }
export interface MemoryMatch { date: string; asOf: string; similarity: number; regime: string; expiryContext: string; matchedFeatures?: string[]; outcome10m: string | null; outcome20m: string | null; outcome30m: string | null }
export interface MemorySummary { available: boolean; reason?: string | null; matchedEpisodes: number; similarityStrength: string; bigMove30HistoricalRate: number | null; directionBias: 'UP' | 'DOWN' | null; directionRate: number | null; historicalOutcome: Record<string, number | null>; topMatches: MemoryMatch[] }
export interface NewsSummary { available: boolean; status: string; monitoringWindow?: string; latestHighImpactEvent?: {eventId: string; category: string; headline: string; firstSeenAt: string; source: string; url: string; relevance: string}; historicalMatches?: number; similarityStrength?: string; historicalBigMove30Rate?: number | null; historicalDirectionBias?: 'UP'|'DOWN'|null; historicalDirectionRate?: number|null; marketConfirmation?: string; confirmationDrivers?: string[] }
export interface IntelligenceMemory { numerical: MemorySummary; news: NewsSummary }
export interface MemoryDay { date: string; session_type: string; expiry_context: string; open: number | null; high: number | null; low: number | null; close: number | null; close_change_percent: number | null; gap_percent: number | null; range_percent: number | null; open_to_close_percent: number | null; direction: string; causalEvidenceGrade: string; timingPrecision: string; tags: string[]; special_flags: string[]; sourceQuality: string; dataQuality: string; whatHappened: string; whyItMoved: string; patternLesson: string; source_block: string; sourceMetadata: Array<{title?: string;url?: string;publication_text?: string}>; checksum: string }
export interface MemoryDaysResponse { available: boolean; count: number; days: MemoryDay[] }
export interface MemoryDayResponse { available: boolean; reason?: string; day?: MemoryDay; markdown?: string }
export interface NewsEvent {event_id:string;session_date:string;event_category:string;event_tags:string[];headline_or_summary:string;known_at:string|null;published_at:string|null;first_seen_at:string|null;knowledge_phase:string;timing_precision:string;causal_evidence_grade:string;predictive_eligible:boolean;context_only:boolean;directional_context:string;source_name:string|null;source_url:string|null;relevance:string;reaction:Record<string,unknown>}
export interface NewsEventsResponse {available:boolean;count:number;events:NewsEvent[]}
export interface NewsStatusResponse {available:boolean;pollingIntervalSeconds:number;monitoringWindow:string;sources:Array<{source:string;state:string;checked_at:string;article_count:number;message?:string}>;lastRefresh:string|null}
export interface IntelligenceResponse { available: boolean; reason: string | null; asOf: string | null; marketRegime: string; sessionBias: SessionBias; qualityStatus: string; featureSetVersion?: string; vixStatus: string; horizons: IntelligenceHorizon[]; banknifty?: BankniftyContext; evidence?: {price: PriceEvidence; futures: FuturesEvidence; options: OptionsEvidence; volatility: VolatilityEvidence}; levels?: ImportantLevels; why?: string[]; memory?: IntelligenceMemory; kronosExperimental: ExperimentalForecast; timeMachineExperimental: ExperimentalForecast }
export interface IntelligenceHistoryResponse { available: boolean; reason: string; items: IntelligenceHorizon[]; page: number; pageSize: number; total: number }
export interface Candle { instrument: string; timestamp: string; open: number; high: number; low: number; close: number; volume: number | null; qualityStatus: string }
export interface Prediction { id: string | null; available: boolean; horizonMinutes: 5 | 10 | 15 | 20; direction: Direction | null; rawClass: Direction | null; rawProbabilities: Record<Direction, number> | null; calibratedProbabilities: Record<Direction, number> | null; confidence: number | null; systemAbstained: boolean; abstentionReasons: string[]; abstentionReason: string | null; algorithm: string | null; model: string | null; modelVersion: string | null; modelChecksum: string | null; calibrationMethod: string | null; calibrationVersion: string | null; reliabilityBucket: Record<string, unknown> | null; asOf: string | null; createdAt: string | null; expiresAt: string | null; actualOutcome: Direction | 'UNKNOWN' | null; qualityStatus: string | null; resolved: boolean }
export interface FuturesSnapshot { available: boolean; reason?: string; contract?: string; expiry?: string; ltp?: number | null; indexPrice?: number | null; premium?: number | null; premiumPercent?: number | null; oi?: number | null; oiChange?: number | null; volume?: number | null; buildup?: string; rollover?: boolean; asOf?: string | null }
export interface DashboardSummary { instrument: string; available: boolean; lastPrice: number | null; priceChange: number | null; priceChangePercent: number | null; latestCandle: Candle | null; marketState: string; dataMode: string; zerodhaStatus: string; pythonStatus: string; future: FuturesSnapshot; selectedOptionExpiry: string | null; unresolvedQualityWarnings: number; live: LiveStatus; timestamp: string; intelligence: IntelligenceResponse }
export interface LiveStatus { connected: boolean; state: string; collectorMode: string; credentialsConfigured: boolean; message: string | null; lastTick: string | null; lastCompletedCandle: string | null; receivedTickCount: number; reconnectCount: number; gapCount: number; activeGapCount: number; desiredSubscriptions: number[]; activeSubscriptions: number[]; startedAt: string | null; stoppedAt: string | null; recentQualityWarning: string | null }
export interface OptionSide { ltp: number; volume: number | null; oi: number | null; oiChange: number | null; bid: number | null; ask: number | null; spread: number | null; qualityStatus: string }
export interface OptionRow { strike: number; atm: boolean; ce?: OptionSide; pe?: OptionSide }
export interface OptionsSnapshot { available: boolean; reason: string | null; expiry: string | null; eligibleExpiries?: string[]; underlying?: number; atmStrike?: number; strikeRange?: number; freshnessStatus?: string; asOf?: string; pcrOi?: number | null; totalCeOi?: number | null; totalPeOi?: number | null; totalCeOiChange?: number | null; totalPeOiChange?: number | null; atmStraddle?: number | null; strongCeOi?: number | null; strongPeOi?: number | null; rows: OptionRow[] }
export interface ModelStatus { horizonMinutes: number; algorithm: string; version: string; status: string; trainingRange: {from: string; to: string}; calibration: string; metrics: Record<string, number> | null; artifactValid: boolean }
export interface V2BinaryMetrics { sampleCount:number; accuracy:number|null; balancedAccuracy:number|null; precision:number|null; recall:number|null; f1:number|null; brier:number|null; logLoss:number|null; classCounts:Record<string,number> }
export interface V2EvaluationMetrics { stageA:V2BinaryMetrics; stageB:V2BinaryMetrics; coverage:number; scoredRows:number; joint:{accuracy:number|null; macroF1:number|null; weightedF1:number|null}; exactCandidateHoldout?:V2EvaluationMetrics }
export interface V2ModelStatus { horizonMinutes:number; status:string; reason?:string|null; algorithm?:string; version?:string; trainingRevision?:string; featureCount?:number; trainingRows?:number; calibration?:Record<string,{status:string;method?:string}>|null; decisionThreshold?:number|null; metrics:V2EvaluationMetrics|null; approvalResult:string; approvalReasons:string[]; artifactChecksum?:string; modelChecksum?:string; evaluationChecksum?:string|null; approvalChecksum?:string|null; policyVersion?:string|null }
export interface V2Comparison { horizonMinutes:number; selectedAlgorithm:string|null; candidates:Record<string,{evaluationChecksum:string; modelChecksum:string; modelVersion:string; utility:number; metrics:Record<string,number|null>}>; selectionReason:{reason:string; rule:string; rejectedAlgorithm:string|null; metricDifferences:Record<string,number|null>} }
export interface ModelsResponse { available:boolean; reason?:string|null; bundleStatus:string; disclaimer:string; models:V2ModelStatus[]; legacyModels:ModelStatus[]; comparisons:V2Comparison[] }
export interface DataQualityIssue { id: string; severity: 'INFO' | 'WARNING' | 'ERROR'; code: string; detectedAt: string; instrumentId: number | null; minuteAt: string | null; resolved: boolean; details: Record<string, unknown> }
export interface QualityResponse { available: boolean; reason: string | null; unresolvedCount: number; issues: DataQualityIssue[] }
export interface HistoryResponse { available: boolean; reason: string; items: Prediction[]; page: number; pageSize: number; total: number }
export interface HealthResponse { symfony: string; python: string; postgres: string; marketCollector: LiveStatus | null; timestamp: string }
export interface Configuration { liveMarketEnabled: boolean; bigMoveRuntimeEnabled: boolean; newsPollingEnabled: boolean; memoryRollupEnabled: boolean; optionStrikeWidth: number; universeRefreshSeconds: number; newsPollSeconds: number; evidenceMinimumSample: number; rawTickRetentionDays: number; currentDataMode: 'LIVE'|'SIMULATED'|'OFFLINE' }
export type ReadinessState = 'READY'|'PARTIAL'|'BLOCKED'|'DISABLED'|'NOT_CONFIGURED';
export type ContextV3Status = 'DEFINED'|'NO_HISTORICAL_FEATURES'|'PARTIAL_COVERAGE'|'READY_FOR_EVALUATION'|'UNAVAILABLE';
export interface ContextV3Cohort {
  datasetRevisionId:string; instrumentId:number; status:ContextV3Status;
  totalCandidateRows:number; passRows:number; coveragePercent:number; tradingDayCount:number; dateRange:string[];
  readyDates:string[]; missingDatesBySource:Record<string,string[]>; ambiguousDates:string[];
  missingBlockCounts:Record<string,number>; sourceConflictRows:number; pinnedLineageAvailable:boolean;
  casBoundaryExclusions:Record<string,number>; indexCloseExclusions:Record<string,number>;
  commonSupportWithPriceCoreV2:Record<string,{rows:number;baselineEligibleRows:number;coveragePercent:number}>;
  minimumCoverage?:Record<string,number>; passRowFraction?:number; tradingDayFraction?:number;
}
export interface ContextV3Coverage {status:ContextV3Status;profiles?:Array<{profileVersion:string;status:ContextV3Status;definitionChecksum:string;cohorts:ContextV3Cohort[]}>;comparisonPolicy?:string}
export type ContextV3RuntimeStatus = 'READY'|'PARTIAL'|'UNAVAILABLE';
export type ContextV3RuntimeProfileStatus = 'PASS'|'PARTIAL'|'UNAVAILABLE';
export type ContextV3RuntimeEvidenceValue = string|number|boolean|null;
export interface ContextV3RuntimeBlock {
  status:string; sourceMaxTimestamp:string|null; missingReasons:string[];
  sourceEvidence:Record<string,ContextV3RuntimeEvidenceValue|Record<string,ContextV3RuntimeEvidenceValue>>;
}
export interface ContextV3RuntimeProfile {
  profileVersion:string; status:ContextV3RuntimeProfileStatus; requiredBlocks:string[];
  horizonEligibility:Record<'10'|'20'|'30',boolean>; blockStatuses:Record<string,string>;
  missingReasons:string[]; sourceMaxTimestamp:string|null; referenceMinute:string|null;
  profileChecksum:string|null; blocks:Record<string,ContextV3RuntimeBlock>;
}
export interface ContextV3Runtime {
  status:ContextV3RuntimeStatus; asOf:string; marketAsOf:string|null;
  profiles:Record<string,ContextV3RuntimeProfile>; reasons:string[];
}
export interface SystemReadiness {
  researchReadiness?:{states:string[];notice:string};
  microstructure?:MicrostructureRuntime;
  shadow?:ShadowSummary;
  observedAt: string;
  categories: Record<'APPLICATION'|'MARKET_DATA'|'MARKET_MEMORY'|'MODEL_RESEARCH'|'MODELS'|'NEWS'|'EVIDENCE'|'RESEARCH_CONTEXT'|'CONSTITUENT_CONTEXT'|'FUTURES_CONTEXT'|'EVENT_IMPACT'|'UPSTOX_INSTRUMENT_NEWS'|'MARKET_STRUCTURE', ReadinessState> & {CONTEXT_V3:ContextV3Status;CONTEXT_V3_RUNTIME:ContextV3RuntimeStatus;CONTEXT_V3_EXPERIMENT:'AVAILABLE'|'NOT_CONFIGURED'|'UNAVAILABLE';SCHEDULED_CONTEXT:'CONFIGURED'|'INVALID'|'NOT_CONFIGURED'};
  blockers: string[];
  application: Record<string, unknown>; calendar: Record<string, unknown>; zerodha: Record<string, unknown>;
  instrumentSnapshot: Record<string, unknown>; live: Record<string, unknown>; marketMemory: Record<string, unknown>;
  research: Record<string, unknown>; models: Array<Record<string, unknown>>; runtime: Record<string, unknown>;
  news: Record<string, unknown> & {scheduledEvents?:'CONFIGURED'|'INVALID'|'NOT_CONFIGURED';scheduledContext?:{status:'CONFIGURED'|'INVALID'|'NOT_CONFIGURED';configuredEventCount:number|null;configuredDateRange:string[];configuredEventTypes:string[];visibleEventCount:number|null}}; evidence: Record<string, unknown>;
  marketStructureContext: {status:string;CONSTITUENT_CONTEXT?:Record<string,unknown>;FUTURES_CONTEXT?:Record<string,unknown>;UPSTOX_INSTRUMENT_NEWS?:Record<string,unknown>;MARKET_STRUCTURE?:Record<string,unknown>};
  eventImpact: Record<string,unknown>;
  contextV3: ContextV3Coverage;
  contextV3Runtime: ContextV3Runtime;
  contextV3Experiment?: ContextV3ExperimentsResponse;
  researchContext: { status: ReadinessState; providers?: Record<string,string>; nextRequiredAction?: string[]; lastRequestStates?: Array<Record<string,unknown>>;
    coverage?: {status:string;canonicalSessions:number;sessionsWithOptionContext:number;sessionsWithNifty50:number;sessionsWithVix:number;sessionsWithGiftNifty:number;sessionsWithBrent:number;sessionsWithUsdInr:number;sessionsWithHistoricalPCR:number;sessionsWithHistoricalOI:number;sessionsWithHistoricalMaxPain:number;contextMinuteRows:number;sourceConflicts:number} };
}

export type ContextV3ExperimentClassification = 'INSUFFICIENT_COVERAGE'|'INSUFFICIENT_EVIDENCE'|'V3_BETTER'|'NO_CLEAR_IMPROVEMENT'|'V3_WORSE';
export interface ContextV3ExperimentSummary {
  version:string; createdAt:string|null; experimentId:string|null; datasetRevisionId:string|null;
  instrumentId:number|null; profileVersion:string|null; dateFrom:string|null; dateTo:string|null;
  classification:ContextV3ExperimentClassification|null; blockers:string[]; runtimeImpact:string|null;
  productionArtifactsModified:boolean|null;
  manifestChecksum?:string; contextGenerationChecksum?:string;
  contextGenerationManifests?:Array<{version:string;checksum:string;referencedRowCount:number;referencedDateCount:number;dates:string[]}>;
  horizons:Record<string,{classification:ContextV3ExperimentClassification|null;status:string;algorithms?:Record<string,ContextV3AlgorithmResult>;evaluation?:{rows:number;dates:number;classCounts:Record<string,number>}}>;
}
export interface ContextV3Metrics {
  stageA:Record<string,number|string|null>; stageB:Record<string,number|string|null>;
  joint:{accuracy:number|null;macroF1:number|null;perClass?:Record<string,Record<string,number|null>>};
}
export interface ContextV3AlgorithmResult {
  classification:ContextV3ExperimentClassification; foldCount:number; evaluatedFoldCount:number;
  pairedRows:number; independentDays:number; predictionChecksum:string;
  aggregate:{baseline:ContextV3Metrics;candidate:ContextV3Metrics}|null;
  exactFinalHoldout:{baseline:ContextV3Metrics;candidate:ContextV3Metrics}|null;
  bootstrap:{status:string;[key:string]:string|number|{lower:number;upper:number;mean?:number}|null};
}
export interface MicrostructureEvidence {
  status:'PASS'|'PARTIAL'|'UNAVAILABLE'; values:Record<string,number|null>; missingReasons:string[];
  sourceMaxTimestamp?:string|null; ageSeconds?:number; depthComplete?:boolean;
}
export interface MicrostructureRuntime {
  status:'PASS'|'PARTIAL'|'UNAVAILABLE'; marketAsOf:string|null; decisionAt:string; schemaVersion:string;
  futures:MicrostructureEvidence|null; options:MicrostructureEvidence|null;
  greeks:{status:'GREEKS_UNAVAILABLE'}; missingReasons:string[];
  freshness:{maximumAgeSeconds:number;availabilityBasis:string}|null;
}
export interface ShadowSummary {
  status:string;captureStatus:string;capturedCount:number;resolvedCount:number;minimumSample:number;minimumSampleState:string;
  sampleWindow:string;notice:string;missingness:Record<string,number>;
  horizons:Record<string,{captured:number;resolved:number;contextAvailable:number;microstructureAvailable:number}>;
  cohorts:Record<string,{captured:number;resolved:number;stageAAccuracy:number|null;stageABrier:number|null}>;
}
export interface ShadowRecent {
  prediction_id:string;market_as_of:string;decision_at:string;horizon_minutes:number;
  context_status:string;microstructure_status:string;state:string;direction:string|null;
  big_move_probability:number;actual_label:string|null;outcome_quality:string|null;
}
export interface ContextV3ExperimentsResponse {available:boolean;reason?:string;count:number;experiments:ContextV3ExperimentSummary[];notice?:string}

export interface EventImpactDetail {
  available:boolean; status:string;
  study?: {event_id:string;event_known_at:string;reference_minute?:string;reference_price?:number;
    pre_event_state:Record<string,unknown>;event_features:Record<string,unknown>;reaction_summary?:Record<string,unknown>};
  historicalReactionPrior?: {kind:'HISTORICAL_REACTION_PRIOR';minimumSample:number;
    horizons:Record<string,{status:string;sampleCount:number;grouping:string[];meanReturn?:number;medianReturn?:number;
      positiveRate?:number;negativeRate?:number;bigMoveRate?:number|null;bigUpRate?:number|null;bigDownRate?:number|null;
      meanMaxUpExcursion?:number|null;meanMaxDownExcursion?:number|null;volatilityExpansionRate?:number|null}>};
}
