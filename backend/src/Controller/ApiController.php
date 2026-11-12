<?php

namespace App\Controller;

use App\Service\PythonClient;
use PDO;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;
use Throwable;

#[Route('/api')]
final class ApiController extends AbstractController
{
    public function __construct(private readonly PythonClient $python) {}

    #[Route('/health', methods: ['GET'])]
    public function health(): JsonResponse
    {
        $python = $this->safeGet('/internal/health', [], false);
        unset($python['_httpStatus']);
        $database = 'NOT_CONFIGURED';
        $databaseUrl = $_ENV['SYMFONY_DATABASE_DSN'] ?? getenv('SYMFONY_DATABASE_DSN');
        if ($databaseUrl) {
            try {
                (new PDO((string) $databaseUrl))->query('SELECT 1');
                $database = 'UP';
            } catch (Throwable) {
                $database = 'DOWN';
            }
        }
        return $this->json([
            'symfony' => 'UP', 'python' => $python['status'] ?? 'DOWN', 'pythonDetails' => $python,
            'postgres' => $database, 'marketCollector' => $python['collector'] ?? null,
            'timestamp' => gmdate(DATE_ATOM),
        ], ($python['status'] ?? null) === 'UP' ? 200 : 503);
    }

    #[Route('/dashboard', methods: ['GET'])]
    public function dashboard(): JsonResponse { return $this->proxy('/internal/dashboard'); }

    #[Route('/market/candles', methods: ['GET'])]
    public function candles(Request $request): JsonResponse
    {
        $interval = $request->query->getString('interval', '1m');
        if ($interval !== '1m') {
            return $this->json(['error' => 'INVALID_INTERVAL', 'message' => 'Only 1m is supported.'], 422);
        }
        return $this->proxy('/internal/candles', [
            'instrument' => $request->query->getString('instrument', 'BANKNIFTY'),
            'interval' => $interval, 'limit' => min(2000, max(1, $request->query->getInt('limit', 500))),
        ]);
    }

    #[Route('/predictions/latest', methods: ['GET'])]
    public function latestPredictions(): JsonResponse { return $this->proxy('/internal/predictions/latest'); }

    #[Route('/predictions/history', methods: ['GET'])]
    public function predictionHistory(Request $request): JsonResponse
    {
        return $this->proxy('/internal/predictions/history', [
            'page' => max(1, $request->query->getInt('page', 1)),
            'page_size' => min(100, max(1, $request->query->getInt('pageSize', 25))),
            'date' => $request->query->get('date'), 'horizon' => $request->query->get('horizon'),
            'direction' => $request->query->get('direction'), 'resolved' => $request->query->get('resolved'),
        ]);
    }

    #[Route('/models/status', methods: ['GET'])]
    public function models(): JsonResponse { return $this->proxy('/internal/models/status'); }
    #[Route('/research/evidence-summary', methods: ['GET'])]
    public function evidenceSummary(): JsonResponse { return $this->proxy('/internal/research/evidence-summary'); }
    #[Route('/research/evidence-journal', methods: ['GET'])]
    public function evidenceJournal(Request $request): JsonResponse
    {
        return $this->proxy('/internal/research/evidence-journal', [
            'page' => max(1, $request->query->getInt('page', 1)),
            'pageSize' => min(100, max(1, $request->query->getInt('pageSize', 25))),
            'date' => $request->query->get('date'), 'horizon' => $request->query->get('horizon'),
            'resolved' => $request->query->get('resolved'),
        ]);
    }
    #[Route('/research/evidence-journal/{prediction_id}', methods: ['GET'])]
    public function evidenceJournalDetail(string $prediction_id): JsonResponse
    {
        return $this->proxy('/internal/research/evidence-journal/'.rawurlencode($prediction_id));
    }
    #[Route('/live/status', methods: ['GET'])]
    public function live(): JsonResponse { return $this->proxy('/internal/live/status'); }
    #[Route('/live/simulation/start', methods: ['POST'])]
    public function startSimulation(): JsonResponse { return $this->proxyPost('/internal/live/simulation/start'); }
    #[Route('/live/simulation/stop', methods: ['POST'])]
    public function stopSimulation(): JsonResponse { return $this->proxyPost('/internal/live/simulation/stop'); }
    #[Route('/market/options', methods: ['GET'])]
    public function options(Request $request): JsonResponse { return $this->proxy('/internal/options', ['expiry' => $request->query->get('expiry')]); }
    #[Route('/market/futures', methods: ['GET'])]
    public function futures(): JsonResponse { return $this->proxy('/internal/futures'); }
    #[Route('/data-quality', methods: ['GET'])]
    public function quality(): JsonResponse { return $this->proxy('/internal/data-quality'); }
    #[Route('/configuration', methods: ['GET'])]
    public function configuration(): JsonResponse { return $this->proxy('/internal/configuration'); }

    #[Route('/system/readiness', methods: ['GET'])]
    public function readiness(): JsonResponse { return $this->proxy('/internal/system/readiness'); }

    #[Route('/research/context/coverage', methods: ['GET'])]
    public function researchContextCoverage(): JsonResponse { return $this->proxy('/internal/research/context/coverage'); }

    #[Route('/research/context-v3/profiles', methods: ['GET'])]
    public function contextV3Profiles(): JsonResponse { return $this->proxy('/internal/research/context-v3/profiles'); }

    #[Route('/research/context-v3/coverage', methods: ['GET'])]
    public function contextV3Coverage(): JsonResponse { return $this->proxy('/internal/research/context-v3/coverage'); }

    #[Route('/research/context-v3/runtime', methods: ['GET'])]
    public function contextV3Runtime(): JsonResponse { return $this->proxy('/internal/research/context-v3/runtime'); }

    #[Route('/research/microstructure/runtime', methods: ['GET'])]
    public function microstructureRuntime(): JsonResponse { return $this->proxy('/internal/research/microstructure/runtime'); }

    #[Route('/research/shadow/summary', methods: ['GET'])]
    public function shadowSummary(): JsonResponse { return $this->proxy('/internal/research/shadow/summary'); }

    #[Route('/research/shadow/recent', methods: ['GET'])]
    public function shadowRecent(): JsonResponse { return $this->proxy('/internal/research/shadow/recent'); }

    #[Route('/research/context-v3/preflight', methods: ['GET'])]
    public function contextV3Preflight(Request $request): JsonResponse
    {
        return $this->proxy('/internal/research/context-v3/preflight', [
            'revision_id' => $request->query->get('revision_id'),
            'instrument_id' => $request->query->get('instrument_id'),
            'from' => $request->query->get('from'),
            'to' => $request->query->get('to'),
            'profiles' => $request->query->get('profiles', 'all'),
        ]);
    }

    #[Route('/research/context-v3/experiments', methods: ['GET'])]
    public function contextV3Experiments(): JsonResponse { return $this->proxy('/internal/research/context-v3/experiments'); }

    #[Route('/research/context-v3/experiments/{id}', methods: ['GET'])]
    public function contextV3Experiment(string $id): JsonResponse { return $this->proxy('/internal/research/context-v3/experiments/'.rawurlencode($id)); }

    #[Route('/research/context/day/{date}', requirements: ['date' => '\d{4}-\d{2}-\d{2}'], methods: ['GET'])]
    public function researchContextDay(string $date): JsonResponse { return $this->proxy('/internal/research/context/day/'.rawurlencode($date)); }
    #[Route('/intelligence/current', methods: ['GET'])]
    public function currentIntelligence(): JsonResponse { return $this->proxy('/internal/intelligence/current'); }

    #[Route('/research/market-structure/coverage', methods: ['GET'])]
    public function marketStructureCoverage(): JsonResponse { return $this->proxy('/internal/research/market-structure/coverage'); }

    #[Route('/research/market-structure/day/{date}', requirements: ['date' => '\d{4}-\d{2}-\d{2}'], methods: ['GET'])]
    public function marketStructureDay(string $date): JsonResponse { return $this->proxy('/internal/research/market-structure/day/'.rawurlencode($date)); }

    #[Route('/research/event-impact/summary', methods: ['GET'], priority: 10)]
    public function eventImpactSummary(): JsonResponse { return $this->proxy('/internal/research/event-impact/summary'); }

    #[Route('/research/event-impact/{id}', methods: ['GET'])]
    public function eventImpactDetail(string $id): JsonResponse { return $this->proxy('/internal/research/event-impact/'.rawurlencode($id)); }
    #[Route('/intelligence/history', methods: ['GET'])]
    public function intelligenceHistory(Request $request): JsonResponse
    {
        return $this->proxy('/internal/intelligence/history', [
            'page' => max(1, $request->query->getInt('page', 1)),
            'page_size' => min(100, max(1, $request->query->getInt('pageSize', 25))),
            'date' => $request->query->get('date'), 'horizon' => $request->query->get('horizon'),
            'direction' => $request->query->get('direction'), 'resolved' => $request->query->get('resolved'),
        ]);
    }

    #[Route('/research/external/current', methods: ['GET'])]
    public function externalResearchCurrent(Request $request): JsonResponse
    {
        return $this->proxy('/internal/research/external/current', [
            'instrument_id' => $request->query->get('instrument_id'),
            'as_of' => $request->query->get('as_of'),
        ]);
    }

    #[Route('/research/external/history', methods: ['GET'])]
    public function externalResearchHistory(Request $request): JsonResponse
    {
        return $this->proxy('/internal/research/external/history', [
            'revision_id' => $request->query->get('revision_id'),
            'instrument_id' => $request->query->get('instrument_id'),
            'model' => $request->query->get('model'),
            'limit' => min(200, max(1, $request->query->getInt('limit', 50))),
        ]);
    }

    #[Route('/research/external/status', methods: ['GET'])]
    public function externalResearchStatus(): JsonResponse
    {
        return $this->proxy('/internal/research/external/status');
    }

    #[Route('/research/external/evaluation', methods: ['GET'])]
    public function externalResearchEvaluation(Request $request): JsonResponse
    {
        return $this->proxy('/internal/research/external/evaluation', [
            'revision_id' => $request->query->get('revision_id'),
            'instrument_id' => $request->query->get('instrument_id'),
        ]);
    }

    #[Route('/memory/current-similarity', methods: ['GET'])]
    public function currentMemorySimilarity(): JsonResponse { return $this->proxy('/internal/memory/current-similarity'); }
    #[Route('/memory/similar', methods: ['GET'])]
    public function similarMemory(): JsonResponse { return $this->proxy('/internal/memory/similar'); }
    #[Route('/memory/coverage', methods: ['GET'])]
    public function memoryCoverage(): JsonResponse { return $this->proxy('/internal/memory/coverage'); }
    #[Route('/news/scheduled', methods: ['GET'])]
    public function scheduledNews(): JsonResponse { return $this->proxy('/internal/news/scheduled'); }
    #[Route('/memory/days', methods: ['GET'])]
    public function memoryDays(): JsonResponse { return $this->proxy('/internal/memory/days'); }
    #[Route('/memory/day/{date}', requirements: ['date' => '\\d{4}-\\d{2}-\\d{2}'], methods: ['GET'])]
    public function memoryDay(string $date): JsonResponse { return $this->proxy('/internal/memory/day/'.$date); }

    #[Route('/news/status', methods: ['GET'])]
    public function newsStatus(): JsonResponse { return $this->proxy('/internal/news/status'); }
    #[Route('/news/latest', methods: ['GET'])]
    public function newsLatest(Request $request): JsonResponse { return $this->proxy('/internal/news/latest', ['limit' => min(100, max(1, $request->query->getInt('limit', 30)))]); }
    #[Route('/news/refresh', methods: ['POST'])]
    public function newsRefresh(): JsonResponse { return $this->proxyPost('/internal/news/refresh'); }
    #[Route('/news/events', methods: ['GET'])]
    public function newsEvents(Request $request): JsonResponse { return $this->proxy('/internal/news/events', [
        'limit' => min(250, max(1, $request->query->getInt('limit', 100))),
        'category' => $request->query->get('category'), 'relevance' => $request->query->get('relevance'),
        'date' => $request->query->get('date'),
    ]); }
    #[Route('/news/events/{id}', methods: ['GET'])]
    public function newsEvent(string $id): JsonResponse { return $this->proxy('/internal/news/events/'.$id); }
    #[Route('/news/event-memory/current', methods: ['GET'])]
    public function currentNewsMemory(): JsonResponse { return $this->proxy('/internal/news/event-memory/current'); }
    #[Route('/news/event-memory/{id}', methods: ['GET'])]
    public function newsEventMemory(string $id): JsonResponse { return $this->proxy('/internal/news/event-memory/'.$id); }

    private function proxy(string $path, array $query = []): JsonResponse
    {
        $payload = $this->safeGet($path, $query);
        return $this->payloadResponse($payload);
    }

    private function proxyPost(string $path): JsonResponse
    {
        try { return $this->payloadResponse($this->python->post($path)); }
        catch (Throwable) { return $this->json($this->unavailable(), 503); }
    }

    private function safeGet(string $path, array $query = [], bool $authenticated = true): array
    {
        try { return $this->python->get($path, $query, $authenticated); }
        catch (Throwable) { return $this->unavailable(); }
    }

    private function unavailable(): array
    {
        return ['error' => 'PYTHON_SERVICE_UNAVAILABLE', 'message' => 'The market-data service is currently unavailable.', 'available' => false, '_httpStatus' => 503];
    }

    private function payloadResponse(array $payload): JsonResponse
    {
        $status = (int) ($payload['_httpStatus'] ?? (isset($payload['error']) ? 503 : 200));
        unset($payload['_httpStatus']);

        return $this->json($payload, $status);
    }
}
