/*
 * Отправка заявки с сайта в платформу вместе с метками и ClientID Метрики.
 * ClientID нужен, чтобы бронь из CRM вернулась в Метрику офлайн-конверсией
 * и стратегия Директа училась на бронях, а не на звонках.
 *
 * Лучше отправлять заявку с сервера сайта (токен не виден посетителям).
 * Если отправляете из браузера, токен защищает только от случайных ботов.
 */
(function () {
  const PLATFORM = 'https://hub.example.com/api/webhooks/form?token=ВАШ_WEBHOOK_TOKEN';

  function cookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  // Метки первого визита сохраняем, чтобы они не потерялись при переходах по сайту.
  const params = new URLSearchParams(location.search);
  const keys = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'yclid'];
  if (keys.some((k) => params.get(k))) {
    try { sessionStorage.setItem('ph_utm', JSON.stringify(Object.fromEntries(keys.map((k) => [k, params.get(k) || ''])))); } catch (e) {}
  }

  window.sendPartyLead = function (form) {
    let utm = {};
    try { utm = JSON.parse(sessionStorage.getItem('ph_utm') || '{}'); } catch (e) {}
    const data = Object.fromEntries(new FormData(form).entries());
    return fetch(PLATFORM, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...utm, ...data, ym_client_id: cookie('_ym_uid'), form: form.getAttribute('name') || 'site' }),
      keepalive: true,
    });
  };
})();

/* Пример:
 * <form name="birthday" onsubmit="sendPartyLead(this)">
 *   <input name="name"> <input name="phone"> <input name="child_age"> <input name="party_date" type="date">
 * </form>
 */
