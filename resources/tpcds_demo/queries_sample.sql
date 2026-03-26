select cs_sold_date_sk
  from catalog_sales_sanitized
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year = 2001 and d_moy > 2;

select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized
       on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
       inner many to one join customer on cs_bill_customer_sk = c_customer_sk
       inner many to one join customer_address on c_current_addr_sk = ca_address_sk
 WHERE d_year between 2000 and 2002 and ca_country = 'UNITED STATES';
