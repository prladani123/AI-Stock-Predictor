<?php

namespace App\Tests;

use Symfony\Bundle\FrameworkBundle\Test\WebTestCase;

final class ApiControllerTest extends WebTestCase
{
    public function testHealthAlwaysReturnsStructuredJson(): void
    {
        $client = static::createClient();
        $client->request('GET', '/api/health');
        self::assertContains($client->getResponse()->getStatusCode(), [200, 503]);
        self::assertJson($client->getResponse()->getContent());
    }
}
