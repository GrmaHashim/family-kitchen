// Family dashboard enhancements: phone-order form helpers and new-order alerts.
(function () {
  const money = (cents) => "\u2066$" + (cents / 100).toFixed(2) + "\u2069";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Auto-submit the date picker in the day bar
  document.querySelectorAll("[data-autosubmit]").forEach((el) => {
    el.addEventListener("change", () => el.form.submit());
  });

  // ------------------------------------------------ new web order alert
  const banner = document.getElementById("newBanner");
  if (banner) {
    const since = banner.dataset.latest || "0";
    const check = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch("/admin/api/pulse?since=" + encodeURIComponent(since), { credentials: "same-origin" });
        if (!res.ok) return;
        const data = await res.json();
        if (data.new_web_orders > 0) {
          document.getElementById("newText").textContent =
            data.new_web_orders === 1 ? "وصل طلب جديد من الموقع" : "وصلت " + data.new_web_orders + " طلبات جديدة من الموقع";
          banner.hidden = false;
          document.title = "(" + data.new_web_orders + ") " + document.title.replace(/^\(\d+\) /, "");
        }
      } catch (e) { /* offline: try again on the next tick */ }
    };
    setInterval(check, 30000);
    document.addEventListener("visibilitychange", check);
  }

  // ------------------------------------------------ order form
  const form = document.getElementById("orderForm");
  if (!form) return;

  const phone = document.getElementById("f-phone");
  const name = document.getElementById("f-name");
  const address = document.getElementById("f-address");
  const suggest = document.getElementById("suggest");
  const deliveryWrap = document.getElementById("deliveryWrap");
  const fee = document.getElementById("f-fee");
  const editing = form.dataset.editing === "1";

  function updateTotal() {
    let cents = 0, count = 0;
    form.querySelectorAll(".tile").forEach((tile) => {
      const q = Math.max(0, parseInt(tile.querySelector("input").value, 10) || 0);
      cents += q * Number(tile.dataset.price);
      count += q;
      tile.classList.toggle("on", q > 0);
    });
    form.querySelectorAll("[data-custom]").forEach((row) => {
      const [n, p, q] = row.querySelectorAll("input");
      const qty = parseInt(q.value, 10) || 0;
      if (n.value.trim() && qty > 0) {
        cents += Math.round((parseFloat(p.value) || 0) * 100) * qty;
        count += qty;
      }
    });
    const delivery = form.querySelector("input[name=fulfillment]:checked");
    if (delivery && delivery.value === "delivery") cents += Math.round((parseFloat(fee.value) || 0) * 100);
    document.getElementById("total").textContent = money(cents);
    document.getElementById("count").textContent = count ? count + " من الأصناف" : "لا أصناف بعد";
  }

  form.addEventListener("click", (e) => {
    const step = e.target.closest("[data-step]");
    if (step) {
      const input = step.parentElement.querySelector("input");
      input.value = Math.max(0, Math.min(99, (parseInt(input.value, 10) || 0) + Number(step.dataset.step)));
      updateTotal();
      return;
    }
    const time = e.target.closest("[data-time]");
    if (time) {
      document.getElementById("f-time").value = time.dataset.time;
      form.querySelectorAll("[data-time]").forEach((b) => b.classList.toggle("on", b === time));
    }
  });
  form.addEventListener("input", updateTotal);
  form.addEventListener("change", (e) => {
    if (e.target.name === "fulfillment") {
      deliveryWrap.hidden = e.target.value !== "delivery";
      updateTotal();
    }
  });

  document.getElementById("addCustom").addEventListener("click", () => {
    const rows = document.getElementById("customRows");
    const copy = rows.lastElementChild.cloneNode(true);
    copy.querySelectorAll("input").forEach((i) => (i.value = ""));
    rows.appendChild(copy);
    copy.querySelector("input").focus();
  });

  // Customer lookup by phone or name
  let timer, lastQuery = "", results = [];
  const digits = (s) => String(s || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");

  function fill(c, withItems) {
    phone.value = c.phone;
    name.value = c.name || name.value;
    if (c.address) address.value = c.address;
    if (withItems) {
      form.querySelectorAll(".tile input").forEach((i) => (i.value = 0));
      const customRows = form.querySelectorAll("[data-custom]");
      let ci = 0;
      c.last_items.forEach((it) => {
        const tile = it.menu_item_id && form.querySelector('.tile[data-id="' + it.menu_item_id + '"]');
        if (tile) {
          tile.querySelector("input").value = it.qty;
        } else {
          let row = customRows[ci] || null;
          if (!row) { document.getElementById("addCustom").click(); row = form.querySelectorAll("[data-custom]")[ci]; }
          const [n, p, q] = row.querySelectorAll("input");
          n.value = it.name; p.value = it.price.toFixed(2); q.value = it.qty;
          ci++;
        }
      });
      const radio = form.querySelector('input[name=fulfillment][value="' + c.last_fulfillment + '"]');
      if (radio) { radio.checked = true; deliveryWrap.hidden = c.last_fulfillment !== "delivery"; }
    }
    updateTotal();
    render();
  }

  function render() {
    const ph = digits(phone.value);
    const exact = ph.length === 10 ? results.find((c) => c.phone_key === ph) : null;
    if (exact) {
      suggest.innerHTML =
        '<div class="known"><span>زبون سابق: <b>' + esc(exact.name) + "</b> · " + exact.orders + " طلب · آخر طلب " + esc(exact.last_date) + "</span>" +
        (!editing && exact.last_items.length ? '<button type="button" class="btn sm" data-repeat>كرّر آخر طلب</button>' : "") + "</div>";
      if (!name.value) name.value = exact.name;
      if (!address.value && exact.address) address.value = exact.address;
      const rep = suggest.querySelector("[data-repeat]");
      if (rep) rep.addEventListener("click", () => fill(exact, true));
      return;
    }
    suggest.innerHTML = results.slice(0, 4).map((c, i) =>
      '<button type="button" class="sug" data-i="' + i + '"><b>' + esc(c.name || "بدون اسم") + '</b><span class="phone" dir="ltr">' +
      esc(c.phone) + "</span><small>" + c.orders + " طلب</small></button>").join("");
    suggest.querySelectorAll(".sug").forEach((b) => b.addEventListener("click", () => fill(results[Number(b.dataset.i)], false)));
  }

  async function lookup(q) {
    if (q === lastQuery) return render();
    lastQuery = q;
    if (q.replace(/\D/g, "").length < 3 && q.trim().length < 2) { results = []; return render(); }
    try {
      const res = await fetch(form.dataset.search + "?q=" + encodeURIComponent(q), { credentials: "same-origin" });
      results = res.ok ? await res.json() : [];
    } catch (e) { results = []; }
    render();
  }

  const onType = (el) => () => { clearTimeout(timer); timer = setTimeout(() => lookup(el.value), 200); };
  phone.addEventListener("input", onType(phone));
  name.addEventListener("input", () => { if (!digits(phone.value)) onType(name)(); });

  form.addEventListener("submit", () => {
    const btn = form.querySelector("[type=submit]");
    setTimeout(() => { btn.disabled = true; btn.textContent = "جارٍ الحفظ…"; }, 0);
  });

  updateTotal();
})();
