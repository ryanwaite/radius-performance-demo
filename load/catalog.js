import http from "k6/http";
import { check, sleep } from "k6";

const baseURL = __ENV.BASE_URL || "http://localhost:8080";

export const options = {
  stages: [
    { duration: "20s", target: 5 },
    { duration: "40s", target: 20 },
    { duration: "60s", target: 40 },
    { duration: "20s", target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const list = http.get(`${baseURL}/api/products?limit=10`, {
    tags: { endpoint: "products-list" },
  });
  check(list, { "list returned 200": (response) => response.status === 200 });

  const productID = (__VU + __ITER) % 10 + 1;
  const product = http.get(`${baseURL}/api/products/${productID}`, {
    tags: { endpoint: "product-get" },
  });
  check(product, { "product returned 200": (response) => response.status === 200 });
  sleep(0.2);
}
