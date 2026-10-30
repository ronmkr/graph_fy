from pathlib import Path
import networkx as nx

from graph_fy.extractors.java import extract_java
from graph_fy.resolvers.feign_rpc import resolve_spring_feign_rpc
from graph_fy.affected import affected_nodes


def test_spring_feign_rpc_cross_microservice_resolution(tmp_path: Path) -> None:
    client_dir = tmp_path / "mall-portal"
    client_dir.mkdir()
    client_file = client_dir / "OrderFeignClient.java"
    client_code = """
package com.macro.mall.portal.client;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

@FeignClient(name = "mall-order", path = "/order")
public interface OrderFeignClient {

    @GetMapping("/{id}")
    OrderDTO getOrderById(@PathVariable("id") Long id);

    @PostMapping("/create")
    OrderDTO createOrder(@RequestBody CreateOrderRequest req);
}
"""
    client_file.write_text(client_code, encoding="utf-8")

    server_dir = tmp_path / "mall-order"
    server_dir.mkdir()
    server_file = server_dir / "OmsOrderController.java"
    server_code = """
package com.macro.mall.order.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/order")
public class OmsOrderController {

    @GetMapping("/{orderId}")
    public OrderDTO getOrder(@PathVariable Long orderId) {
        return null;
    }

    @PostMapping("/create")
    public OrderDTO generateOrder(@RequestBody CreateOrderRequest req) {
        return null;
    }
}
"""
    server_file.write_text(server_code, encoding="utf-8")

    client_res = extract_java(client_file)
    server_res = extract_java(server_file)

    all_nodes = list(client_res.get("nodes", [])) + list(server_res.get("nodes", []))
    all_edges = list(client_res.get("edges", [])) + list(server_res.get("edges", []))

    resolve_spring_feign_rpc(all_nodes, all_edges, root=tmp_path)

    feign_rpc_edges = [e for e in all_edges if e.get("relation") == "calls_feign_rpc"]
    assert len(feign_rpc_edges) == 2

    # Verify edge connectivity
    get_edge = next(e for e in feign_rpc_edges if "getorderbyid" in e["source"])
    assert "getorder" in get_edge["target"]

    create_edge = next(e for e in feign_rpc_edges if "createorder" in e["source"])
    assert "generateorder" in create_edge["target"]

    # Verify reverse blast radius
    g = nx.DiGraph()
    for n in all_nodes:
        g.add_node(n["id"], **n)
    for e in all_edges:
        g.add_edge(e["source"], e["target"], **e)

    # When controller endpoint generateOrder changes, Feign client method createOrder is flagged
    ctrl_create_node = create_edge["target"]
    hits = affected_nodes(g, ctrl_create_node, depth=2)
    hit_ids = [h.node_id for h in hits]
    assert create_edge["source"] in hit_ids


def test_spring_feign_disambiguation_by_service_name(tmp_path: Path) -> None:
    client_dir = tmp_path / "portal"
    client_dir.mkdir()
    client_file = client_dir / "UserFeignClient.java"
    client_code = """
package com.client;

@FeignClient(name = "auth-service", path = "/users")
public interface UserFeignClient {
    @GetMapping("/current")
    UserDTO getCurrentUser();
}
"""
    client_file.write_text(client_code, encoding="utf-8")

    auth_dir = tmp_path / "auth-service"
    auth_dir.mkdir()
    auth_file = auth_dir / "AuthController.java"
    auth_code = """
package com.auth;

@RestController
@RequestMapping("/users")
public class AuthController {
    @GetMapping("/current")
    public UserDTO getCurrentUser() { return null; }
}
"""
    auth_file.write_text(auth_code, encoding="utf-8")

    other_dir = tmp_path / "other-service"
    other_dir.mkdir()
    other_file = other_dir / "OtherController.java"
    other_code = """
package com.other;

@RestController
@RequestMapping("/users")
public class OtherController {
    @GetMapping("/current")
    public UserDTO getCurrentUser() { return null; }
}
"""
    other_file.write_text(other_code, encoding="utf-8")

    all_nodes = []
    all_edges = []
    for f in (client_file, auth_file, other_file):
        res = extract_java(f)
        all_nodes.extend(res.get("nodes", []))
        all_edges.extend(res.get("edges", []))

    resolve_spring_feign_rpc(all_nodes, all_edges, root=tmp_path)

    feign_rpc_edges = [e for e in all_edges if e.get("relation") == "calls_feign_rpc"]
    assert len(feign_rpc_edges) == 1
    # Matched to auth-service AuthController, not other-service
    assert "authcontroller" in feign_rpc_edges[0]["target"]
