"use strict";

let ticket = window.location.hash.slice(1);
// The fragment never reaches HTTP access logs. Remove it from browser history too.
window.history.replaceState(null, "", "/authorize");
const form = document.getElementById("authorization");
const details = document.getElementById("details");
const result = document.getElementById("result");
const input = document.getElementById("token");

async function post(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "omit",
    cache: "no-store",
    body: JSON.stringify(data),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error);
  return payload;
}

post("/api/authorize/info", { ticket }).then((project) => {
  details.textContent = `${project.name} · ${project.url} → ${project.channel}（Zulip 用户 ID：${project.user_id}）`;
  form.hidden = false;
}).catch(() => {
  details.textContent = "链接无效、已过期或服务不可用。请回到 Zulip 获取新链接。";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = input.value;
  input.value = "";
  form.hidden = true;
  result.textContent = "正在验证 GitHub 访问权限…";
  try {
    const response = await post("/api/authorize", { ticket, token });
    result.textContent = response.message;
  } catch (error) {
    result.textContent = error.message || "提交失败，请回到 Zulip 获取新链接。";
  } finally {
    ticket = "";
  }
});
