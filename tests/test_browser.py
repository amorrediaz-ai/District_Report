"""Browser smoke tests against an isolated PostgreSQL schema, never production.

Leaflet 1.9.4 is served from a pinned npm package; map tiles are mocked so tests
neither depend on third-party availability nor download OSM tiles in bulk.
"""
import os
from pathlib import Path
from threading import Thread

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from test_postgresql import module, clean, citizen_client, admin_client, post, PNG


@pytest.mark.parametrize('viewport', [{'width': 1365, 'height': 900}, {'width': 390, 'height': 844}], ids=['desktop', 'mobile'])
def test_map_gallery_and_notifications_in_browser(module, viewport):
    citizen_client(module)
    admin = admin_client(module)
    server = make_server('127.0.0.1', 0, module.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    dist = Path(os.environ.get('LEAFLET_DIST', '../browser-test-runtime/node_modules/leaflet/dist')).resolve()
    assert (dist / 'leaflet.js').is_file()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL'))
            context = browser.new_context(viewport=viewport)
            context.route('https://unpkg.com/leaflet@1.9.4/dist/**', lambda route: route.fulfill(
                path=str(dist / ('leaflet.css' if route.request.url.endswith('.css') else 'leaflet.js')),
                content_type='text/css' if route.request.url.endswith('.css') else 'application/javascript',
                headers={'Access-Control-Allow-Origin': '*'}))
            context.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PNG, content_type='image/png'))
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/login')
            header = page.get_by_role('banner')
            menu = header.locator('summary')
            mobile = viewport['width'] < 1040
            expect(header.get_by_role('link', name='Сообщить о проблеме')).to_be_visible()
            if mobile:
                menu.click()
            expect(header.get_by_role('link', name='Карта', exact=True)).to_be_visible()
            expect(header.get_by_role('link', name='Войти')).to_have_attribute('aria-current', 'page')
            if mobile:
                page.keyboard.press('Escape')
                expect(menu).to_be_focused()
                expect(header.get_by_role('link', name='Карта')).to_have_count(0)
            page.locator('input[name=email]').fill('owner@example.test')
            page.locator('input[name=password]').fill('citizen-password123')
            page.get_by_role('button', name='Войти', exact=True).click()
            page.wait_for_url(base + '/')
            menu.click()
            expect(header.get_by_role('link', name='Панель администратора')).to_have_count(0)
            header.get_by_role('link', name='Мои обращения').click()
            page.wait_for_url(base + '/my')
            header.get_by_role('link', name='Сообщить о проблеме').click()
            page.locator('input[name=title]').fill('Тест карты и галереи')
            page.locator('input[name=location]').fill('Астана, пешеходная дорожка')
            page.locator('select[name=category]').select_option('Дороги и тротуары')
            page.locator('textarea[name=description]').fill('Повреждено покрытие на пешеходной дорожке.')
            map_element = page.locator('#location_map.leaflet-container')
            expect(map_element).to_be_visible()
            map_element.click(position={'x': 110, 'y': 130})
            assert page.locator('#latitude').input_value()
            page.locator('#photo_input').set_input_files([
                {'name': f'photo{i}.png', 'mimeType': 'image/png', 'buffer': PNG} for i in range(3)])
            expect(page.locator('#photo_preview img')).to_have_count(3)
            page.get_by_role('button', name='Отправить обращение', exact=True).click()
            page.wait_for_url('**/report/*')
            rid = int(page.url.rsplit('/',1)[1])
            expect(page.locator('.photo_gallery img')).to_have_count(3)
            expect(page.locator('#location_map.leaflet-container')).to_be_visible()
            page.get_by_role('link', name='Редактировать обращение', exact=True).click()
            page.get_by_label('Удалить фото 1', exact=True).check()
            page.locator('#photo_input').set_input_files([
                {'name': f'new{i}.png', 'mimeType': 'image/png', 'buffer': PNG} for i in range(2)])
            page.get_by_role('button', name='Сохранить изменения', exact=True).click()
            page.wait_for_url(base + f'/report/{rid}')
            expect(page.locator('.photo_gallery img')).to_have_count(4)
            post(admin, f'/report/{rid}/status', status='В работе', message='Бригада назначена.')
            page.reload()
            header.get_by_role('link', name='Уведомления').click()
            page.wait_for_url(base + '/notifications')
            expect(page.locator('#notification_count')).to_have_text('1')
            expect(page.get_by_text('Бригада назначена.', exact=True)).to_be_visible()
            page.get_by_role('button', name=f'Открыть обращение № {rid}', exact=True).click()
            page.wait_for_url(base + f'/report/{rid}')
            expect(page.locator('#notification_count')).to_be_hidden()
            page.goto(base + '/map')
            expect(page.locator('#location_map .leaflet-interactive')).to_have_count(1)
            page.locator('#location_map .leaflet-interactive').click()
            expect(page.locator('.leaflet-popup-content')).to_contain_text('Тест карты и галереи')
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            menu.click()
            header.get_by_role('button', name='Выйти').click()
            page.goto(base + '/login')
            page.locator('input[name=email]').fill('admin@example.test')
            page.locator('input[name=password]').fill('test-admin-password-123')
            page.get_by_role('button', name='Войти', exact=True).click()
            menu.click()
            admin_link = header.get_by_role('link', name='Панель администратора')
            expect(admin_link).to_be_visible()
            admin_link.click()
            page.wait_for_url(base + '/admin')
            assert errors == []
            context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
