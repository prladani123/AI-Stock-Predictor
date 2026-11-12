FROM composer:2 AS vendor
WORKDIR /app
COPY composer.json composer.lock ./
RUN composer install --no-dev --prefer-dist --no-interaction --no-progress --no-scripts --no-autoloader

FROM php:8.3-cli-alpine
RUN apk add --no-cache curl libpq-dev && docker-php-ext-install pdo_pgsql
WORKDIR /app
COPY --from=vendor /app/vendor ./vendor
COPY . .
COPY --from=vendor /usr/bin/composer /usr/local/bin/composer
RUN composer dump-autoload --no-dev --optimize --no-scripts && mkdir -p var && chown -R www-data:www-data var
USER www-data
EXPOSE 8080
CMD ["php", "-S", "0.0.0.0:8080", "-t", "public"]
