"use strict";
document.querySelectorAll("#customerTable th").forEach((header, index) => {
  let ascending = true;
  header.querySelector("button").addEventListener("click", () => {
    const table = header.closest("table");
    const body = table.tBodies[0];
    const grades = {VIP: 1, GOLD: 2, SILVER: 3, NORMAL: 4};
    Array.from(body.rows).sort((a, b) => {
      const left = a.cells[index].textContent.trim();
      const right = b.cells[index].textContent.trim();
      const order = header.dataset.type === "grade"
        ? (grades[left] || 99) - (grades[right] || 99)
        : left.localeCompare(right, "ko");
      return ascending ? order : -order;
    }).forEach(row => body.appendChild(row));
    table.querySelectorAll("th").forEach(th => th.removeAttribute("aria-sort"));
    header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    ascending = !ascending;
  });
});
