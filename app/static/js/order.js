// Customer order form: quantity steppers, live total, delivery toggle, time slots per day.
(function () {
  const form = document.getElementById("orderForm");
  if (!form) return;

  const money = (cents) => "\u2066$" + (cents / 100).toFixed(2) + "\u2069";
  const slots = JSON.parse(document.getElementById("slotData").textContent || "{}");
  const fee = JSON.parse(document.getElementById("feeData").textContent || "0");
  const slotSelect = document.getElementById("slotSelect");
  const noSlots = document.getElementById("noSlots");
  const deliveryWrap = document.getElementById("deliveryWrap");

  function fmtTime(t) {
    let [h, m] = t.split(":").map(Number);
    const suffix = h >= 12 ? "م" : "ص";
    h = h % 12 || 12;
    return h + ":" + String(m).padStart(2, "0") + " " + suffix;
  }

  function updateTotal() {
    let cents = 0, count = 0;
    form.querySelectorAll(".dish").forEach((row) => {
      const input = row.querySelector("input[type=number]");
      if (!input) return;
      const q = Math.max(0, Math.min(99, parseInt(input.value, 10) || 0));
      cents += q * Number(row.dataset.price);
      count += q;
      row.classList.toggle("on", q > 0);
    });
    const delivery = form.querySelector("input[name=fulfillment]:checked");
    if (count && delivery && delivery.value === "delivery") cents += fee;
    document.getElementById("total").textContent = money(cents);
    document.getElementById("count").textContent = count ? count + " من الأصناف" : "اختر من القائمة";
  }

  function updateSlots() {
    const day = form.querySelector("input[name=order_date]:checked");
    const list = (day && slots[day.value]) || [];
    const keep = slotSelect.value || slotSelect.dataset.selected;
    slotSelect.innerHTML = "";
    list.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = fmtTime(s);
      if (s === keep) opt.selected = true;
      slotSelect.appendChild(opt);
    });
    slotSelect.disabled = !list.length;
    noSlots.hidden = list.length > 0;
  }

  function updateFulfillment() {
    const checked = form.querySelector("input[name=fulfillment]:checked");
    const isDelivery = checked && checked.value === "delivery";
    deliveryWrap.hidden = !isDelivery;
    const hint = document.getElementById("pickupHint");
    if (hint) hint.hidden = isDelivery;
    updateTotal();
  }

  form.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-step]");
    if (!btn) return;
    const input = btn.parentElement.querySelector("input");
    input.value = Math.max(0, Math.min(99, (parseInt(input.value, 10) || 0) + Number(btn.dataset.step)));
    updateTotal();
  });
  form.addEventListener("input", updateTotal);
  form.addEventListener("change", (e) => {
    if (e.target.name === "order_date") updateSlots();
    if (e.target.name === "fulfillment") updateFulfillment();
  });
  form.addEventListener("submit", () => {
    const btn = form.querySelector("[type=submit]");
    setTimeout(() => { btn.disabled = true; btn.textContent = "جارٍ الإرسال…"; }, 0);
  });

  updateSlots();
  updateFulfillment();
})();
