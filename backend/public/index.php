<?php

use App\Kernel;

$_SERVER['APP_RUNTIME_OPTIONS']['disable_dotenv'] = !is_file(dirname(__DIR__).'/.env');
require_once dirname(__DIR__).'/vendor/autoload_runtime.php';

return static function (array $context): Kernel {
    return new Kernel($context['APP_ENV'], (bool) $context['APP_DEBUG']);
};
