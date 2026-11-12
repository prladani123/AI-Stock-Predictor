<?php

namespace App\Service;

use Symfony\Contracts\HttpClient\HttpClientInterface;

final class PythonClient
{
    public function __construct(
        private readonly HttpClientInterface $http,
        private readonly string $baseUrl,
        private readonly string $token,
    ) {
    }

    public function get(string $path, array $query = [], bool $authenticated = true): array
    {
        return $this->request('GET', $path, $query, $authenticated);
    }

    public function post(string $path): array
    {
        return $this->request('POST', $path);
    }

    private function request(string $method, string $path, array $query = [], bool $authenticated = true): array
    {
        $options = ['timeout' => $path === '/internal/news/refresh' ? 40.0 : 10.0, 'query' => array_filter($query, static fn ($v) => $v !== null && $v !== '')];
        if ($authenticated) {
            $options['headers'] = ['X-Internal-Token' => $this->token];
        }
        $response = $this->http->request($method, rtrim($this->baseUrl, '/').$path, $options);
        $status = $response->getStatusCode();
        $payload = $response->toArray(false);
        $payload['_httpStatus'] = $status;

        return $payload;
    }
}
