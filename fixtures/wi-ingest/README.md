# WI ingest fixtures

Tiny catalog HTML, one-lake 24K Hydro GeoJSON, and minimal PDFs for `pipeline` unit tests. CI never hits the network.

Ada Lake on the live DNR catalog uses map key `0417300a` and WBIC `0417300`.

`catalog-pager-encoded.html` is a pager fragment with HTML-entity apostrophes around the ASP.NET Next postback target.

`catalog-map-keys.html` covers Hartman vs Chain O' Lakes map-key collisions, multi-county shared PDFs, Clear/North SWIMS media types, Amber counties, and Plover River row counts.

Altoona Lake uses a SWIMS `DownloadDocument` URL with map key `2128100a` and a committed JPEG (`2128100a.jpg`), not a PDF.