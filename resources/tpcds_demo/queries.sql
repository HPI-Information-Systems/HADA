select sum(cr_return_amount)
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year between 2000 and 2002;


select sum(cr_return_amount)
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_date = '2000-01-01';

select sum(cr_return_amount)
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910;

select sum(cr_return_amount)
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910
   AND cs_item_sk > 12345;

select sum(cr_return_amount)
  from catalog_returns
       inner many to one join catalog_sales on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910;


select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year between 2000 and 2002;


select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
       inner many to one join customer on cs_bill_customer_sk = c_customer_sk
       inner many to one join customer_address on c_current_addr_sk = ca_address_sk
 WHERE d_year between 2000 and 2002 and ca_country = 'UNITED STATES';


select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_date = '2000-01-01'
 WITH HINT(USE_HEX_PLAN);

select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910 and cs_bill_customer_sk > 1234 and cs_bill_customer_sk < 43210;

 select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910;

select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910 AND cs_sales_price > 123;

select cr_order_number
  from catalog_returns_sanitized
       inner many to one join catalog_sales_sanitized on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910
   AND cs_item_sk > 12345;

select cr_order_number
  from catalog_returns
       inner many to one join catalog_sales on cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
 WHERE cs_sold_date_sk between 2450815 and 2451910;


select sum(cs_sales_price)
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_date between '2000-01-01' and '2002-01-01';


select sum(cs_sales_price)
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_date between '1998-01-01' and '2002-12-31';


select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_date between '1998-01-01' and '2002-12-31';


select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year = 2000;


select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_moy = 7;

select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year = 2001 and d_moy > 2 and d_moy < 11;

 select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year = 2001 and d_moy = 2;


select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
 WHERE d_year = 2001 and d_moy > 2;


select cs_sold_date_sk
  from catalog_sales
       inner many to one join date_dim on cs_sold_date_sk = d_date_sk
WHERE d_year = 2001 and d_moy = 2 and d_dom > 2 and d_dom < 23;
